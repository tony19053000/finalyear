import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
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
 * The controls the campaign's state allows (campaignVerbs). Cancel asks
 * first (a native dialog): it stops the campaign's running benchmark jobs and
 * skips every open run - it cannot be undone. Each button names its campaign
 * for screen readers (a list has one row of buttons per campaign).
 */
export function CampaignActions({ campaignId, name, state, runnerOwned, size = "sm" }: CampaignActionsProps) {
  const control = useCampaignControl(campaignId);
  const [confirming, setConfirming] = useState(false);
  const verbs = campaignVerbs(state, runnerOwned);
  if (verbs.length === 0) return <span className="text-xs text-fg-subtle">—</span>;
  const pendingVerb = control.isPending ? control.variables : undefined;
  const run = (verb: CampaignVerb) => {
    control.mutate(verb, {
      onSettled: () => {
        if (verb === "cancel") setConfirming(false);
      },
    });
  };
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {verbs.map((verb) => (
        <Button
          key={verb}
          size={size}
          variant={verb === "cancel" ? "danger" : verb === "start" || verb === "resume" ? "primary" : "secondary"}
          aria-label={`${VERB_LABELS[verb]} campaign ${name}`}
          loading={pendingVerb === verb}
          disabled={control.isPending && pendingVerb !== verb}
          onClick={(event) => {
            event.stopPropagation();
            if (verb === "cancel") setConfirming(true);
            else run(verb);
          }}
        >
          {VERB_LABELS[verb]}
        </Button>
      ))}
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
