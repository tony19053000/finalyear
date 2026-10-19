/**
 * One campaign control (start / pause / resume / cancel) with its toasts:
 * success -> the server's new state is written into the cache and the
 * campaign's entries refetch; a refusal (409 conflict - e.g. a finished
 * campaign, `campaign_running` -, 403, 503, a network error) -> a toast with
 * the server's message and code, nothing changed locally.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { describeError } from "../../api/errors";
import { useToast } from "../../components/toastContext";
import { controlCampaign, type CampaignControlOut, type CampaignList, type CampaignVerb } from "./benchApi";
import { benchKeys } from "./benchKeys";
import { VERB_DONE, VERB_LABELS } from "./campaignView";

export function useCampaignControl(campaignId: string) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationKey: [...benchKeys.campaign(campaignId), "control"],
    mutationFn: (verb: CampaignVerb) => controlCampaign(campaignId, verb),
    onSuccess: (out: CampaignControlOut, verb) => {
      client.setQueryData<CampaignList>(benchKeys.list, (list) =>
        list === undefined
          ? list
          : {
              ...list,
              campaigns: list.campaigns.map((c) =>
                c.campaign_id === campaignId ? { ...c, state: out.state, counts: out.counts } : c,
              ),
            },
      );
      void client.invalidateQueries({ queryKey: benchKeys.list, exact: true });
      void client.invalidateQueries({ queryKey: benchKeys.status(campaignId), exact: true });
      void client.invalidateQueries({ queryKey: benchKeys.campaign(campaignId) });
      const extra = verb === "cancel" ? ` ${String(out.cancelled_jobs)} benchmark job(s) told to stop.` : "";
      toast({ title: VERB_DONE[verb], description: `${campaignId} is ${out.state}.${extra}`, tone: "ok" });
    },
    onError: (error, verb) => {
      const { message, code } = describeError(error);
      toast({
        title: `${VERB_LABELS[verb]} refused`,
        description: code !== undefined ? `${message} (${code})` : message,
        tone: "failed",
      });
    },
  });
}
