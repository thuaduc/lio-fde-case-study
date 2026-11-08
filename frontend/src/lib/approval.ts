import type { ProcurementRequest } from '@/types/database';

type ApprovalFields = Pick<ProcurementRequest, 'approval_status' | 'auto_approved'>;

/** Sentence-case approval label; auto-approvals (org threshold) get their own. */
export function approvalLabel(request: ApprovalFields): string {
  if (request.auto_approved) return 'Auto-approved';
  const s = request.approval_status;
  return s.charAt(0).toUpperCase() + s.slice(1);
}
