// ============================================================================
// Database Type Definitions
// ============================================================================

export type Profile = {
  id: string;
  email: string;
  full_name: string | null;
  avatar_url: string | null;
  created_at: string;
  updated_at: string;
};

export type Organization = {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  updated_at: string;
};

export type OrganizationMember = {
  id: string;
  organization_id: string;
  user_id: string;
  role: 'admin' | 'buyer' | 'requester';
  created_at: string;
};

export type OrganizationInvite = {
  id: string;
  organization_id: string;
  email: string;
  role: 'admin' | 'buyer' | 'requester';
  invited_by: string;
  expires_at: string;
  accepted_at: string | null;
  created_at: string;
};

export type CommodityGroup = {
  id: number;
  category: string;
  name: string;
  /** Booking guidance; the classifier treats it as binding. */
  description?: string | null;
  archived?: boolean;
  created_at?: string;
};

export type ProcurementRequest = {
  id: string;
  user_id: string;
  organization_id: string | null;
  created_by: string | null;
  requestor_name: string;
  title: string;
  vendor_name: string;
  vat_id: string | null;
  commodity_group_id: number | null;
  department: string | null;
  status: 'open' | 'in_progress' | 'closed';
  approval_status: 'pending' | 'approved' | 'rejected';
  approved_by: string | null;
  approved_at: string | null;
  // Approved by the org's approval threshold rather than by a buyer.
  auto_approved?: boolean;
  total_cost: number | null;
  created_at: string;
  updated_at: string;
  // Rich-extraction fields (camelCase on the wire).
  quoteNumber?: string | null;
  validUntil?: string | null;
  currency?: string | null;
  netTotal?: number | null;
  taxTotal?: number | null;
  extractionWarnings?: (import('@/lib/api').WarningDetail | string)[] | null;
  fieldSources?: import('@/lib/api').FieldSources | null;
};

export type OrderLine = {
  id: string;
  request_id: string;
  position_description: string;
  unit_price: number;
  amount: number;
  unit: string;
  total_price: number;
  line_order: number;
  created_at: string;
  key: string;
  parentKey?: string | null;
  kind?: 'item' | 'surcharge' | 'discount' | 'shipping' | 'fee' | 'alternative' | 'optional';
  position?: string | null;
  vendorItemNumber?: string | null;
  listUnitPrice?: number | null;
  adjustmentPercent?: number | null;
  sources?: { page: number; x0: number; y0: number; x1: number; y1: number }[];
  sourceText?: string | null;
  articleId?: string | null;
  originalQuote?: import('@/lib/api').OriginalQuote | null;
  commodityGroupId?: number | null;
};

// ============================================================================
// Helper Types for Joined Queries
// ============================================================================

export type ProcurementRequestWithDetails = ProcurementRequest & {
  order_lines: OrderLine[];
  commodity_groups: CommodityGroup | null;
};

export type OrganizationWithRole = Organization & {
  role: 'owner' | 'admin' | 'member';
};

export type OrganizationMemberWithProfile = OrganizationMember & {
  profiles: Profile | null;
};

