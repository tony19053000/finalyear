import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { cn } from "../../lib/cn";
import type { CampaignState, CampaignVerb } from "./benchApi";
import { VERB_LABELS, campaignVerbs } from "./campaignView";
import { useCampaignControl } from "./useCampaignControl";

export interface CampaignActionsProps {
  campaignId: string;
  name: string;
  state: CampaignState;
  /** From the campaign's status; undefined while it loads. */
  runnerOwned: boolean | undefined;
  size?: "sm" | "md";
}

/**
 * In a list row: quiet mono text actions, right-aligned (Start / Resume in
 * honey, the others muted and honey on hover). On the campaign page: the job page's round actions - honey
 * outline for Start / Resume, a quiet outline for Pause, the ghost Cancel.
 */
function verbClass(verb: CampaignVerb, size: "sm" | "md"): string {
  if (size === "sm") return cn("bm-act", (verb === "start" || verb === "resume") && "bm-act-honey");
  if (verb === "start" || verb === "resume") return "jb-honey-btn bm-btn";
  if (verb === "cancel") return "nd-ghost-btn bm-btn";
  return "jb-quiet-btn bm-btn";
}

/**
 * The controls the campaign's state allows (campaignVerbs). Cancel asks
 * first (a native dialog): it stops the campaign's running benchmark jobs and
 * skips every open run - it cannot be undone. Each button names its campaign
 * for screen readers (a list has one row of buttons per campaign). While one
 * control runs it is busy and the others are disabled.
 */
export function CampaignActions({ campaignId, name, state, runnerOwned, size = "sm" }: CampaignActionsProps) {
  const control = useCampaignControl(campaignId);
  const [confirming, setConfirming] = useState(false);
  const verbs = campaignVerbs(state, runnerOwned);
  if (verbs.length === 0) return <span className="text-fg-subtle">—</span>;
  const pendingVerb = control.isPending ? control.variables : undefined;
  const run = (verb: CampaignVerb) => {
    control.mutate(verb, {
      onSettled: () => {
        if (verb === "cancel") setConfirming(false);
      },
    });
  };
  return (
    <div className={cn("flex items-center", size === "sm" ? "flex-nowrap justify-end gap-x-6" : "flex-wrap gap-3")}>
      {verbs.map((verb) => {
        const loading = pendingVerb === verb;
        return (
          <button
            key={verb}
            type="button"
            className={verbClass(verb, size)}
            aria-label={`${VERB_LABELS[verb]} campaign ${name}`}
            aria-busy={loading || undefined}
            disabled={loading || (control.isPending && pendingVerb !== verb)}
            onClick={(event) => {
              event.stopPropagation();
              if (verb === "cancel") setConfirming(true);
              else run(verb);
            }}
          >
            {loading && <span aria-hidden="true" className="size-2.5 animate-spin rounded-full border border-current border-t-transparent" />}
            {VERB_LABELS[verb]}
          </button>
        );
      })}
      <Dialog
        open={confirming}
        onClose={() => {
          if (!control.isPending) setConfirming(false);
        }}
        title="Cancel this campaign?"
        description={`"${name}" (${campaignId}) is ${state}. Its running benchmark jobs are cancelled and every run not yet done is skipped. This cannot be undone; the records measured so far stay.`}
        footer={
          <>
            <Button
              onClick={() => {
                setConfirming(false);
              }}
              disabled={control.isPending}
            >
              Keep running
            </Button>
            <Button
              variant="danger"
              loading={pendingVerb === "cancel"}
              onClick={() => {
                run("cancel");
              }}
            >
              Cancel campaign
            </Button>
          </>
        }
      />
    </div>
  );
}
