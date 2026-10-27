// Server state for every feature (TanStack Query). Keys are scoped by the signed-in key so switching tenant never
// shows another tenant's cached data.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { session } from "../../features/auth/session";
import { api, post } from "./client";
import type { Answer, GuidelineVersion, InvoiceDetail, Me, QueueItem } from "./types";

const who = () => session.get()?.key ?? "anonymous";

export const useMe = () => useQuery({ queryKey: [who(), "me"], queryFn: () => api<Me>("/v1/me"), retry: false });

export const useQueue = (status?: string) =>
  useQuery({
    queryKey: [who(), "queue", status ?? "all"],
    queryFn: () => api<QueueItem[]>(`/v1/invoices${status ? `?status=${status}` : ""}`),
    refetchInterval: 15_000,
  });

export const useInvoice = (number: string | undefined) =>
  useQuery({
    queryKey: [who(), "invoice", number],
    queryFn: () => api<InvoiceDetail>(`/v1/invoices/${encodeURIComponent(number ?? "")}`),
    enabled: Boolean(number),
  });

export function useDecide(number: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (decisions: Record<string, { decision: "accept" | "reject"; note?: string }>) =>
      post<{ status: string; total_adjustment: string; pending: number }>(`/v1/invoices/${encodeURIComponent(number)}/decisions`, { decisions }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: [who(), "invoice", number] });
      qc.invalidateQueries({ queryKey: [who(), "queue"] });
    },
  });
}

export const useGuidelines = () => useQuery({ queryKey: [who(), "guidelines"], queryFn: () => api<GuidelineVersion[]>("/v1/guidelines") });

export function useApprove() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) => post<{ version: number; status: string }>(`/v1/guidelines/${version}/approve`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: [who(), "guidelines"] }),
  });
}

export const useAsk = () => useMutation({ mutationFn: (question: string) => post<Answer>("/v1/ask", { question }) });

export const useEvaluation = () =>
  useQuery({ queryKey: [who(), "evaluation"], queryFn: () => api<Record<string, any>>("/v1/evaluation"), staleTime: 60_000 });

export interface AnnotationItem {
  id: string;
  kind: string;
  action: string;
  clause_id: string;
  clause_text: string;
  line_no: string;
  timekeeper_role: string;
  hours: string;
  description: string;
  explanation: string;
}

export const useAnnotationItems = () =>
  useQuery({ queryKey: [who(), "annotation"], queryFn: () => api<AnnotationItem[]>("/v1/annotation/items"), staleTime: Infinity });

export function useAppeal(number: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { finding_id: string; message: string }) =>
      post<{ intent: string; outcome: string; reply: string | null }>(`/v1/invoices/${encodeURIComponent(number)}/appeals`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: [who(), "invoice", number] }),
  });
}

export function useDecideAppeal(number: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: number; decision: "uphold" | "reverse" }) => post(`/v1/appeals/${id}/decision`, { decision }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [who(), "invoice", number] }),
  });
}
