# MeshForge upgrade/rollback functions for the .deb maintainer scripts
# (V0.17 G5, D393/D403, Q35/Q38/Q39). POSIX sh. Not a script of its own:
# scripts/build_deb.py inlines this file where a maintainer script has the
# include line naming it (see build_deb.expand_script; dpkg runs each script on
# its own, so the shared code is copied in at build time - one source, both
# packages).
#
# The including script sets, before calling anything:
#   MF_OPT      program folder holding `current`: /opt/meshforge (agent) or
#               /opt/meshforge/controller (controller)
#   MF_UNIT     the systemd unit (meshforge-agent.service / ...-controller...)
#   MF_VERSION  this package's bundle version (filled in by build_deb.py)
#
# Layout (all root-owned, under the root-only program folder - never under
# /var/lib/meshforge, which the service account and its jobs own):
#   $MF_OPT/.previous/<ver>     the version that ran before this upgrade,
#                               a hard-link copy (dpkg replaces files by rename,
#                               so the copy costs no disk and stays intact)
#   $MF_OPT/.upgrade/state      the upgrade in progress (key=value lines)
#   $MF_OPT/.upgrade/rolled-back.json   the last upgrade was rolled back
#   $MF_OPT/.upgrade/interrupted.json   a start found a half-done upgrade
#   $MF_OPT/.upgrade/fail-upgrade-to    Q38 trigger: fail the upgrade to <ver>
#
# Lifecycle:
#   preinst upgrade     mf_upgrade_begin     keep the running tree, state=unpacking
#   postinst configure  mf_upgrade_switch    current -> this version, state=configuring
#                       (restart + health check by the package's own postinst)
#                       mf_upgrade_done      success: forget .previous and the state
#                       mf_upgrade_rollback  failure: current -> .previous/<old>
#   ExecStartPre=+      mf_upgrade_recover   a half upgrade left by a power loss
#   prerm/postrm remove mf_upgrade_forget    nothing of this stays behind

MF_BOOT_ID_FILE=/proc/sys/kernel/random/boot_id

mf_say() { printf '%s: %s\n' "${MF_UNIT%.service}" "$*"; }

# Root-owned folders and files only (the unit tests redefine these two after
# including this file; nothing here reads the environment).
mf_install_dir() { install -d -m 0755 -o root -g root "$1"; }
mf_root_owned() { [ "$(stat -c %u "$1" 2>/dev/null)" = 0 ]; }

mf_upg() { printf '%s/.upgrade' "$MF_OPT"; }
mf_prev() { printf '%s/.previous' "$MF_OPT"; }

mf_boot_id() { cat "$MF_BOOT_ID_FILE" 2>/dev/null || echo unknown; }

mf_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# JSON string escaping for the few values written (versions, short reasons).
mf_json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n\r'; }

mf_state_get() { # key -> value from the state file ("" if none)
	file="$(mf_upg)/state"
	[ -f "$file" ] || return 0
	sed -n "s/^$1=//p" "$file" | head -n 1
}

mf_write_state() { # from to phase previous
	upg=$(mf_upg)
	mf_install_dir "$upg"
	tmp="$upg/.state.$$"
	{
		printf 'from=%s\n' "$1"
		printf 'to=%s\n' "$2"
		printf 'phase=%s\n' "$3"
		printf 'previous=%s\n' "$4"
		printf 'boot=%s\n' "$(mf_boot_id)"
		printf 'at=%s\n' "$(mf_now)"
	} >"$tmp"
	chmod 0644 "$tmp"
	mv -f "$tmp" "$upg/state"
}

mf_write_record() { # name action from to reason
	upg=$(mf_upg)
	mf_install_dir "$upg"
	tmp="$upg/.$1.$$"
	printf '{"package": "%s", "action": "%s", "from": "%s", "to": "%s", "reason": "%s", "at": "%s"}\n' \
		"$(mf_json_str "${MF_UNIT%.service}")" "$(mf_json_str "$2")" "$(mf_json_str "$3")" \
		"$(mf_json_str "$4")" "$(mf_json_str "$5")" "$(mf_now)" >"$tmp"
	chmod 0644 "$tmp"
	mv -f "$tmp" "$upg/$1"
}

# The pointer swap: `current` names $1 (a path relative to $MF_OPT) or nothing.
# rename(2) over the old link: no moment without a pointer, never two.
mf_point_at() {
	tmp="$MF_OPT/.current.$$"
	rm -f "$tmp"
	ln -s "$1" "$tmp"
	mv -fT "$tmp" "$MF_OPT/current"
}

# The version folder `current` names now, relative to $MF_OPT ("" if none).
mf_current_target() {
	readlink "$MF_OPT/current" 2>/dev/null || true
}

mf_free_kb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $4 }'; }

# preinst upgrade: keep what runs now so a failed upgrade can switch back.
mf_upgrade_begin() {
	running=$(mf_current_target)
	prev=$(mf_prev)
	keep=""
	from=""
	case "$running" in
	"") ;;
	.previous/*)
		# A rolled-back laptop: the previous copy IS what runs; keep it.
		from=${running#.previous/}
		[ -d "$MF_OPT/$running" ] && keep="$running"
		;;
	*/*) mf_say "warning: $MF_OPT/current -> $running is not ours; no rollback copy kept" ;;
	*)
		from=$running
		if [ -f "$MF_OPT/$running/BUILDINFO.json" ]; then
			mf_install_dir "$prev"
			rm -rf "$prev/.tmp"
			if cp -al "$MF_OPT/$running" "$prev/.tmp" 2>/dev/null; then
				:
			else
				rm -rf "$prev/.tmp"
				need=$(du -sk "$MF_OPT/$running" 2>/dev/null | awk '{ print $1 }')
				free=$(mf_free_kb "$prev")
				if [ -n "$need" ] && [ -n "$free" ] && [ "$free" -gt $((need + 51200)) ] &&
					cp -a "$MF_OPT/$running" "$prev/.tmp"; then
					:
				else
					rm -rf "$prev/.tmp"
					mf_say "warning: not enough free disk to keep version $running for a rollback;"
					mf_say "this upgrade cannot switch back by itself if the new version fails."
				fi
			fi
			if [ -d "$prev/.tmp" ]; then
				rm -rf "${prev:?}/$running"
				mv "$prev/.tmp" "$prev/$running"
				keep=".previous/$running"
			fi
		fi
		;;
	esac
	# one previous version only (Q35: pruned to one)
	if [ -d "$prev" ]; then
		for entry in "$prev"/* "$prev"/.[!.]*; do
			[ -e "$entry" ] || continue
			[ ".previous/${entry##*/}" = "$keep" ] && continue
			rm -rf "$entry"
		done
	fi
	mf_write_state "$from" "$MF_VERSION" unpacking "$keep"
}

# postinst configure, before the (re)start: this version runs from now on.
mf_upgrade_switch() {
	if [ "$(mf_current_target)" != "$MF_VERSION" ]; then
		mf_point_at "$MF_VERSION"
	fi
	if [ -f "$(mf_upg)/state" ]; then
		mf_write_state "$(mf_state_get from)" "$MF_VERSION" configuring "$(mf_state_get previous)"
	fi
}

# Q38: the admin's trigger names this version (or "*").
mf_forced_failure() {
	file="$(mf_upg)/fail-upgrade-to"
	[ -f "$file" ] && [ ! -L "$file" ] || return 1
	mf_root_owned "$file" || return 1
	want=$(head -n 1 "$file" | tr -d ' \t\r')
	[ "$want" = "$MF_VERSION" ] || [ "$want" = "*" ]
}

# The upgrade worked: no rollback copy, no state, no old failure notice.
mf_upgrade_done() {
	rm -rf "$(mf_prev)"
	rm -f "$(mf_upg)/state" "$(mf_upg)/rolled-back.json" "$(mf_upg)/interrupted.json"
}

# Is a previous version kept for this upgrade? (prints its path, relative)
mf_upgrade_previous() {
	previous=$(mf_state_get previous)
	# only `.previous/<version>`: one plain name, never `..` or a `/` that could
	# lead `current` out of the program folder (tester G5-T2)
	case "$previous" in
	.previous/*) name=${previous#.previous/} ;;
	*) return 0 ;;
	esac
	case "$name" in
	"" | . | .. | */* | *[!A-Za-z0-9.+~_-]*) return 0 ;;
	esac
	if [ -f "$MF_OPT/$previous/BUILDINFO.json" ]; then
		printf '%s' "$previous"
	fi
}

# The new version failed its health check ($1 = why). Stop it, point `current`
# at the kept previous version and start that ($2 = "norestart": only the
# pointer - the process that runs now IS the previous version, e.g. the new
# version never started). The caller checks its health.
# Returns 1 (nothing changed) when no previous version is kept.
mf_upgrade_rollback() {
	previous=$(mf_upgrade_previous)
	if [ -z "$previous" ]; then
		return 1
	fi
	from=$(mf_state_get from)
	restart=yes
	[ "${2:-}" = norestart ] && restart=no
	if [ "$restart" = yes ] && [ -d /run/systemd/system ]; then
		systemctl stop "$MF_UNIT" >/dev/null 2>&1 || true
	fi
	mf_point_at "$previous"
	mf_write_record rolled-back.json rolled_back "$from" "$MF_VERSION" "$1"
	mf_write_state "$from" "$MF_VERSION" rolled_back "$previous"
	# a forced failure is one-shot: the next install of this version is real
	if mf_forced_failure; then
		rm -f "$(mf_upg)/fail-upgrade-to"
	fi
	if [ "$restart" = yes ] && [ -d /run/systemd/system ]; then
		systemctl daemon-reload || true
		systemctl reset-failed "$MF_UNIT" >/dev/null 2>&1 || true
		systemctl start "$MF_UNIT" || true
	fi
	return 0
}

# After a rollback whose previous version is healthy: keep the copy (it is
# what runs now) but end the upgrade.
mf_upgrade_rolled_back_ok() {
	rm -f "$(mf_upg)/state"
}

# ExecStartPre=+ (as root, before every start): a power loss in the middle of
# an upgrade leaves the state file from another boot. Never fails the start.
#   phase unpacking   -> dpkg may have replaced part of the old files and the
#                        new version was never configured; dpkg itself marks
#                        the package half-installed, which `dpkg --configure -a`
#                        cannot fix: run the kept previous version (reverted)
#                        and tell the owner to install the new package again
#                        (`dpkg -i ./<new .deb>`, which repairs a half-installed
#                        or unpacked package - `apt install --reinstall` refuses
#                        it, measured), whose postinst switches
#                        `current` to the new version and checks it.
#   phase configuring -> the new version was completely unpacked and switched
#                        on: let it start (finishing); the confirmation comes
#                        from `dpkg --configure -a`.
#   same boot         -> an upgrade is running right now (its postinst
#                        restarts the service): do nothing.
mf_upgrade_recover() {
	[ -f "$(mf_upg)/state" ] || return 0
	phase=$(mf_state_get phase)
	boot=$(mf_state_get boot)
	[ "$boot" = "$(mf_boot_id)" ] && return 0
	from=$(mf_state_get from)
	to=$(mf_state_get to)
	case "$phase" in
	unpacking)
		previous=$(mf_upgrade_previous)
		if [ -n "$previous" ]; then
			if [ "$(mf_current_target)" != "$previous" ]; then
				mf_point_at "$previous"
			fi
			mf_write_record interrupted.json reverted "$from" "$to" "power loss or killed installer during the unpack"
			mf_say "an upgrade to $to was interrupted; running the previous version $from (finish it: sudo dpkg -i ./<the $to .deb>, then sudo apt --fix-broken install if apt asks)" >&2
		else
			mf_write_record interrupted.json incomplete "$from" "$to" "no previous version kept"
			mf_say "an upgrade to $to was interrupted and no previous version was kept; starting what is installed (finish it: sudo dpkg -i ./<the $to .deb>, then sudo apt --fix-broken install if apt asks)" >&2
		fi
		;;
	configuring)
		mf_write_record interrupted.json finishing "$from" "$to" "power loss or killed installer after the switch"
		mf_say "an upgrade to $to was interrupted after the switch; starting $to (confirm it: sudo dpkg --configure -a)" >&2
		;;
	*) return 0 ;; # already recovered / rolled back: nothing more to do
	esac
	# the record says what happened; a later boot must not act again
	mf_write_state "$from" "$to" "recovered-$phase" "$(mf_state_get previous)"
	return 0
}

# remove/purge: no rollback copy, no upgrade records, no trigger.
mf_upgrade_forget() {
	rm -rf "$(mf_prev)" "$(mf_upg)"
	rm -f "$MF_OPT"/.current.*
}
