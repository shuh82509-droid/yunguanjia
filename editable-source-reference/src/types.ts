export interface Asset {
  etag?: string
  id: number
  filename: string
  object_key: string
  media_type: 'video' | 'image'
  size: number
  modified_at: string
  category: string
  content_type: string
  status: string
  asset_scope: 'marketing_video' | 'product_image'
  library_type: 'source' | 'remix'
  asset_subtype: string
  folder_name: string
  tags: string[]
  favorite: boolean
  favorite_count: number
  preview_url: string
  download_url: string
  cover_url: string
  source: string
  account_name: string
  ingest_source: 'oa_upload' | 'oss_scan'
  reference_url: string
  reference_video_key: string
  reference_video_name: string
  reference_video_url: string
  material_description: string
  performance_screenshots: { object_key: string; filename: string; url: string }[]
  can_manage: boolean
  can_delete: boolean
  deleted_at?: string | null
  deleted_by_name?: string
  purge_after?: string | null
  purged_at?: string | null
  purge_error?: string
  can_purge?: boolean
  historical_gmv_yuan?: number | null
  gmv_target_count: number
  gmv_updated_at?: string | null
  platform_gmv: { platform: 'qianchuan' | 'adq' | 'channels'; label: string; gmv_yuan: number; updated_at?: string | null }[]
  effective: boolean
  effective_marked_by_name: string
  effective_marked_at?: string | null
  review_status?: 'pending' | 'approved' | 'rejected' | ''
  review_version?: number
  review_note?: string
  review_updated_at?: string | null
}

export interface EffectiveClipLibraryStatus {
  assetId: number
  state: 'not_imported' | 'source_imported' | 'technical_attention' | 'approved' | string
  clipCount: number
  approvedCount: number
  technicalAttentionCount: number
  contentReviewInherited: boolean
  updatedAt?: string | null
}

export interface Stats {
  total: number
  oss_total: number
  videos: number
  images: number
  source_materials: number
  remix_outputs: number
  product_images: number
  favorites: number
  trash: number
  new_this_week: number
  storage_bytes: number
  indexed_records: number
  source: string
  source_updated_at?: string | null
  hit_materials: number
  effective_materials: number
}

export interface UploadTicket {
  object_key: string
  upload_url: string
  public_url: string
  headers: Record<string, string>
  expires_in: number
}

export interface MultipartUploadedPart {
  part_number: number
  etag: string
  size: number
}

export interface MultipartUploadSession {
  session_id: string
  object_key: string
  filename: string
  file_size: number
  part_size: number
  total_parts: number
  status: 'active' | 'completed' | 'cancelled' | 'expired' | string
  uploaded_parts: MultipartUploadedPart[]
  expires_at: string
  public_url: string
}

export interface MultipartPartUrl {
  part_number: number
  upload_url: string
  headers: Record<string, string>
  expires_in: number
}

export interface JianyingDevice {
  id: string
  device_name: string
  active: boolean
  last_seen_at?: string | null
  created_at: string
}

export interface JianyingPairing {
  id: string
  code: string
  status: string
  expires_at: string
  scheme_url: string
  api_base: string
}

export interface JianyingImportTask {
  ticket_id: string
  asset_id: number
  filename: string
  status: 'waiting' | 'claimed' | 'downloading' | 'downloaded' | 'opening' | 'importing' | 'completed' | 'failed' | 'expired' | string
  progress: number
  message: string
  downloaded_bytes: number
  total_bytes: number
  speed_bps: number
  eta_seconds: number
  helper_version: string
  cache_hit: boolean
  created_at?: string
  updated_at: string
  completed_at?: string | null
  expires_at: string
  scheme_url?: string
}

export interface SyncStatus {
  state: 'idle' | 'running' | 'completed' | 'failed'
  processed: number
  total: number
  started_at?: string | null
  completed_at?: string | null
  error: string
}

export interface FacetItem {
  name: string
  count: number
}

export interface ProductImageCategory extends FacetItem {
  cover_url: string
}

export interface Facets {
  categories: FacetItem[]
  content_types: FacetItem[]
  statuses: FacetItem[]
  ingest_sources: FacetItem[]
  library_types: FacetItem[]
  asset_subtypes: FacetItem[]
  folders: FacetItem[]
  tags: FacetItem[]
  directories: FacetItem[]
  source: string
  source_updated_at?: string | null
}

export interface QianchuanStatus {
  configured: boolean
  authorized: boolean
  message: string
  app_id: string
  authorized_at?: string | null
  refresh_valid_until?: string | null
  redirect_uri: string
  can_authorize: boolean
  capabilities: {
    account_material_upload: boolean
    full_domain_plan_add: boolean
    standard_plan_direct_add: boolean
    material_report: boolean
  }
  metrics_sync?: {
    mode?: 'daily' | string
    timezone?: string
    daily_hour?: number
    lookback_days?: number
    daily_last_completed_date?: string | null
    daily_target_date?: string | null
    daily_state?: string
    daily_started_at?: string | null
    daily_completed_at?: string | null
    daily_task_count?: number
    daily_group_count?: number
    daily_processed_groups?: number
    daily_error_groups?: number
    daily_next_run_at?: string | null
    auto_interval_minutes: number
    auto_batch_size: number
    auto_last_run_at?: string | null
    auto_next_run_at?: string | null
    auto_state: string
    record_min_age_minutes: number
    manual_batch_size: number
    manual_cooldown_minutes: number
    manual_running: boolean
    manual_available: boolean
    manual_state: string
    manual_task_count: number
    manual_requested_at?: string | null
    manual_next_available_at?: string | null
    manual_completed_at?: string | null
  }
}

export interface QianchuanDailyMetric {
  stat_date: string
  metrics: Record<string, number | string>
  status: 'success' | 'no_data' | 'error' | 'missing' | string
  has_data: boolean
  link_verified: boolean
  message: string
  error_code: string
  request_id: string
  synced_at: string
}

export interface QianchuanAccount {
  id: string
  name: string
  role: string
  company: string
}

export interface QianchuanProductPlanRule {
  advertiser_id: string
  advertiser_name: string
  scope: 'multiplication' | 'full_domain' | 'standard' | 'all'
  match_mode: 'keyword' | 'exact_plan' | 'all'
  keyword: string
  plan_id: string
}

export interface QianchuanProductPlanBundle {
  key: string
  label: string
  aliases: string[]
  rules: QianchuanProductPlanRule[]
}

export interface QianchuanProductPlanMap {
  source: {
    title: string
    url: string
    document_id: string
    revision: number
    verified_at: string
    department_account_source: {
      title: string
      url: string
      revision: number
      rule: string
    }
  }
  items: QianchuanProductPlanBundle[]
}

export interface QianchuanPlan {
  id: string
  name: string
  status: string
  marketing_scene: string
  campaign_scene: string
  campaign_id: string
  marketing_goal: string
  plan_type: 'standard' | 'full_domain' | 'multiplication'
  plan_type_label: string
  status_label: string
  can_attach_video: boolean
  direct_add_api: string
  is_full?: boolean
  capacity_status?: 'available' | 'full' | string
  capacity_message?: string
  capacity_checked_at?: string
}

export interface QianchuanPlanResult {
  items: QianchuanPlan[]
  warnings: string[]
  complete: boolean
  cached: boolean
  source_read_at: string
  scope: 'all' | 'multiplication' | 'full_domain' | 'standard'
  counts: {
    total: number
    multiplication: number
    full_domain: number
    standard: number
    full?: number
  }
}

export interface QianchuanPlanMaterial {
  video_id: string
  material_id: string
  title: string
  audit_status: string
  material_status: string
  is_delete: boolean
  active: boolean
  delivery_ready: boolean
  needs_reactivation: boolean
  delivery_not_reason: unknown[]
  metrics: Record<string, number | string>
  has_data: boolean
  linked_asset?: { asset_id: number; asset_name: string; task_id: string } | null
}

export interface QianchuanPlanMaterialResult {
  items: QianchuanPlanMaterial[]
  total: number
  active_count: number
  inactive_count: number
  start_date: string
  end_date: string
  pages: number
  request_id: string
  source: string
  read_at: string
  message: string
}

export interface QianchuanTarget {
  advertiser_id: string
  advertiser_name: string
  plan_id: string
  plan_name: string
  plan_type: string
}

export interface PushPreference {
  id: number
  platform: 'qianchuan' | 'adq'
  account_id: string
  account_name: string
  target_id: string
  target_name: string
  target_type: string
  pinned: boolean
  use_count: number
  last_used_at?: string | null
  updated_at: string
}

export interface QianchuanTask {
  id: string
  batch_id: string
  asset_id: number
  asset_name: string
  created_by_number: string
  created_by_name: string
  can_manage: boolean
  can_cancel: boolean
  advertiser_id: string
  advertiser_name: string
  plan_id: string
  plan_name: string
  plan_type: string
  platform_asset_id: string
  upload_task_id: string
  idempotency_key: string
  delivery_entity_type: string
  delivery_entity_id: string
  binding_evidence: Record<string, unknown>
  binding_verified_at?: string | null
  status: string
  message: string
  request_id: string
  attempt_count: number
  last_error_category: string
  failure_stage: string
  error_code: string
  error_message: string
  error_advice: string
  metrics: Record<string, number | string>
  related_ad_ids: string[]
  related_creative_ids: string[]
  metrics_link_status: 'pending' | 'linked_pending' | 'verified' | 'missing' | 'mismatch' | 'unverified' | 'account_material' | string
  metrics_start_date: string
  metrics_end_date: string
  metrics_synced_at?: string | null
  metrics_message: string
  metrics_data_status: 'fresh' | 'no_data' | 'partial' | 'partial_error' | 'pending' | 'error' | 'legacy' | string
  metrics_fresh_through?: string | null
  metrics_coverage: { completed: number; expected: number }
  daily_metrics: QianchuanDailyMetric[]
  created_at: string
  updated_at: string
}

export interface AdqStatus {
  configured: boolean
  authorized: boolean
  message: string
  app_id: string
  account_id: string
  authorized_at?: string | null
  account?: AdqAccount
  can_authorize_user?: boolean
  user_authorization?: {
    authorized: boolean
    message: string
    expires_at?: string | null
  }
  account_metric_accounts?: { account_id: string; account_name: string }[]
  capabilities: {
      original_video_upload: boolean
      dynamic_creative_add: boolean
      shared_library_upload?: boolean
      material_report: boolean
    purchase_and_roi_report: boolean
  }
  metrics_policy: {
    interval_minutes: number
    auto_batch_size: number
    manual_batch_size: number
    manual_cooldown_minutes: number
  }
}

export interface AdqAccountMaterialMetrics {
  account_id: string
  metrics: Record<string, number>
  has_data: boolean
  row_count: number
  video_count: number
  videos: { video_id: string; video_name: string; asset_id?: number | null; row_count: number; metrics: Record<string, number> }[]
  total: number
  page: number
  page_size: number
  total_pages: number
  start_date: string
  end_date: string
  source: string
  message: string
}

export interface AdqAccount {
  account_id: string
  account_name: string
  corporation_name: string
  system_status: string
  business_unit_id?: string
  business_unit_name?: string
}

export interface AdqCampaign {
  campaign_id: string
  campaign_name: string
  configured_status: string
  campaign_type: string
  promoted_object_type: string
  created_time?: number | null
  last_modified_time?: number | null
}

export interface AdqHierarchyAccount extends AdqAccount {
  active: boolean | null
  campaign_total: number | null
  active_campaign_total: number | null
  campaigns: AdqCampaign[]
  complete: boolean
  message: string
}

export interface AdqBusinessUnit {
  business_unit_id: string
  business_unit_name: string
  seed_account_id: string
  configured_account_total: number
  account_total: number | null
  active_account_total: number | null
  campaign_total: number | null
  accounts: AdqHierarchyAccount[]
  complete: boolean
  message: string
}

export interface AdqHierarchy {
  subject: { subject_id: string; subject_name: string }
  catalog: {
    configured: boolean
    authorized: boolean
    message: string
    subject_id: string
    business_unit_total: number
    configured_account_total: number
    authorized_at?: string | null
  }
  business_units: AdqBusinessUnit[]
  business_unit_total: number
  active_account_total: number | null
  campaign_total: number | null
  expected_active_account_total: number
  expected_campaign_total: number
  count_matches_reference: boolean
  complete: boolean
  errors: { business_unit_id: string; account_id: string; stage: string; message: string; category: string }[]
  message: string
}

export interface AdqAccountCatalog {
  items: AdqAccount[]
  total: number
  configured_total: number
  discovered_total: number
  unavailable: { account_id: string; message: string; category: string }[]
  discovery_error: string
  complete: boolean
  message: string
}

export interface AdqAdgroup {
  adgroup_id: string
  adgroup_name: string
  campaign_id: string
  configured_status: string
  system_status: string
  can_attach_video: boolean
  source_dynamic_creative_id: string
  creative_template_id: string
  delivery_mode: string
  dynamic_creative_type: string
}

export interface AdqTarget {
  account_id: string
  account_name: string
  adgroup_id: string
  adgroup_name: string
  source_dynamic_creative_id: string
}

export interface AdqDailyMetric {
  date: string
  metrics: Record<string, number | string>
  status: string
  has_data: boolean
  message: string
  synced_at: string
}

export interface AdqTask {
  id: string
  batch_id: string
  asset_id: number
  asset_name: string
  created_by_number: string
  created_by_name: string
  can_manage: boolean
  account_id: string
  account_name: string
  adgroup_id: string
  adgroup_name: string
  source_dynamic_creative_id: string
  dynamic_creative_id: string
  platform_asset_id: string
  root_material_id: string
  cover_id: string
  binding_evidence: Record<string, unknown>
  binding_verified_at?: string | null
  library_readback_status: 'verified' | 'pending' | 'unavailable' | string
  library_readback_message: string
  library_readback_at?: string | null
  status: string
  message: string
  request_id: string
  attempt_count: number
  last_error_category: string
  failure_stage: string
  error_code: string
  error_message: string
  error_advice: string
  metrics: Record<string, number | string>
  metrics_status: string
  metrics_start_date: string
  metrics_end_date: string
  metrics_synced_at?: string | null
  metrics_message: string
  daily_metrics: AdqDailyMetric[]
  created_at: string
  updated_at: string
}

export interface OaUser {
  realName: string
  number: string
  groupName: string
  deptJobName?: string
  avatar?: string | null
}

export interface OaPermissions {
  private_assets?: boolean
  asset_admin: boolean
  asset_delete_manager: boolean
  super_admin: boolean
  operation_admin: boolean
  manage_permissions: boolean
  video_request_assigner: boolean
  video_request_supervisor_viewer: boolean
  review_config_admin: boolean
  can_mark_effective: boolean
  reviewer_roles: string[]
}

export interface ReviewRoleMember {
  id: number
  role_code: string
  role_label: string
  user_name: string
  user_number: string
  department: string
  center: string
  group_name: string
  source: 'manual' | 'organization_sync' | string
  active: boolean
  created_by_name: string
  created_at: string
}

export interface ReviewRoleConfig {
  code: 'member' | 'team_lead' | 'supervisor' | 'brand_tone' | 'director' | 'internal_control'
  label: string
  is_stage: boolean
  legacy: boolean
  required: boolean
  members: ReviewRoleMember[]
}

export interface ReviewAiRule {
  code: string
  category: 'platform' | 'internal' | 'artist' | 'relaxation'
  severity: 'hard' | 'warning'
  title: string
  pattern: string
  enabled: boolean
  sort_order: number
  updated_by_name: string
  updated_at: string
}

export interface ReviewWorkflowConfig {
  enabled: boolean
  required_roles: string[]
  roles: ReviewRoleConfig[]
  missing_required_roles: string[]
  can_enable: boolean
  updated_by_name: string
  updated_at: string
  viewer_roles: string[]
  can_configure: boolean
  eligible_reviewers: ReviewEligibleReviewer[]
  organization_centers: Array<{
    center: string
    member_count: number
    team_leads: string[]
    supervisors: string[]
    groups: Array<{ group_name: string; member_count: number; team_leads: string[] }>
  }>
  organization_source: { title: string; whiteboard_label: string; document_url: string; document_revision: number }
  fixed_stages: string[]
  naming_standard: {
    enabled: boolean
    blocking: boolean
    version: string
    label: string
    document_url: string
    categories: Array<{ code: ReviewNamingCategory; label: string; rule: string }>
    pending_categories: string[]
  }
  ai_review_enabled: boolean
  ai_service_configured: boolean
  ai_redlines: {
    version: string
    rules: ReviewAiRule[]
    mode: 'advisory'
    blocking: false
    enabled_count: number
    hard_count: number
    warning_count: number
    policy_source: { title: string; url: string; case_count: number }
  }
  brand_tone: { enabled: boolean; blocking: boolean; label: string; document_url: string }
}

export interface ReviewEligibleReviewer {
  user_name: string
  user_number: string
  center: string
  group_name: string
  role_codes: string[]
  role_labels: string[]
}

export type ReviewNamingCategory = 'face' | 'mechanism' | 'product_display' | 'ai_first_creation'
export type ReviewNamingUsage = 'complete' | 'clip'
export type ReviewNamingPosition = 'opening' | 'middle' | 'ending'

export interface ReviewNamingEvidence {
  category: ReviewNamingCategory | ''
  material_name: string
  usage: ReviewNamingUsage | ''
  position: ReviewNamingPosition | ''
  source_asset_id?: number | null
}

export interface ReviewNamingCheck {
  status: 'disabled' | 'passed' | 'failed' | 'not_applicable' | 'stale'
  version: string
  document_url: string
  filename: string
  message: string
  no_applicable_sources: boolean
  evidence_count: number
  required_names: string[]
  missing_names: string[]
  pending_categories: string[]
}

export interface ReviewNamingContext {
  asset_id: number
  filename: string
  library_type: 'source' | 'remix'
  source_suggestions: Array<{ source_asset_id: number; material_name: string; asset_subtype: string }>
  standard: ReviewWorkflowConfig['naming_standard']
}

export interface ReviewDecision {
  role_code: string
  role_label: string
  status: 'pending' | 'approved' | 'rejected'
  reviewer_number: string
  reviewer_name: string
  candidate_reviewers: Array<{ user_name: string; user_number: string; center: string; group_name: string }>
  note: string
  quality_scores: Record<string, number>
  quality_total?: number | null
  quality_grade: string
  decided_at?: string | null
}

export interface ReviewAiFinding {
  rule_code: string
  category: 'platform' | 'internal' | 'artist' | 'relaxation'
  severity: 'hard' | 'warning'
  title: string
  evidence: string
  evidence_source: string
  start_seconds: number
  end_seconds: number
  segment_index: number
  confidence: string
  policy_effect?: 'relaxed' | 'attention'
  recommendation?: string
  source_url?: string
}

export interface ReviewAiResult {
  status: 'disabled' | 'pending' | 'processing' | 'passed' | 'warning' | 'rejected' | 'error' | 'legacy_skipped'
  summary: string
  provider: string
  task_id: string
  rule_version: string
  category_counts: Record<'platform' | 'internal' | 'artist' | 'relaxation', number>
  findings: ReviewAiFinding[]
  segments: Array<Record<string, string | number>>
  error_message: string
  retry_count: number
  started_at?: string | null
  completed_at?: string | null
}

export interface ReviewSubmission {
  id: string
  asset_id: number
  asset_name: string
  preview_url: string
  version: number
  status: 'pending' | 'approved' | 'rejected'
  submitted_by_number: string
  submitted_by_name: string
  note: string
  filename_snapshot: string
  naming_evidence: ReviewNamingEvidence[]
  naming_check: ReviewNamingCheck
  submitted_at: string
  completed_at?: string | null
  current_role: string
  current_role_label: string
  route_center: string
  route_group: string
  assignment_mode: 'organization' | 'designated'
  designated_reviewer_number: string
  designated_reviewer_name: string
  current_reviewers: Array<{ user_name: string; user_number: string; center: string; group_name: string }>
  can_review: boolean
  can_retry_ai: boolean
  ai_review: ReviewAiResult
  brand_tone: { status: 'reserved'; label: string; blocking: boolean; document_url: string }
  decisions: ReviewDecision[]
}

export interface VideoRequestDelivery {
  id: string
  version: number
  submission_id: string
  asset_id: number
  filename: string
  preview_url: string
  cover_url: string
  category: string
  content_type: string
  asset_subtype: string
  folder_name: string
  tags: string[]
  note: string
  uploaded_by_name: string
  created_at: string
}

export interface VideoRequestEvent {
  id: number
  action: string
  actor_name: string
  detail: Record<string, unknown>
  created_at: string
}

export interface VideoRequestMetrics {
  state: 'pending' | 'available'
  message: string
  updated_at?: string | null
  qianchuan?: { task_count: number; gmv_yuan?: number | null; target_count: number; updated_at?: string | null } | null
  adq?: { task_count: number; data_task_count: number; updated_at?: string | null } | null
  channels?: {
    task_count: number
    data_task_count: number
    view_count?: number | null
    like_count?: number | null
    comment_count?: number | null
    share_count?: number | null
    order_count?: number | null
    gmv_yuan?: number | null
    updated_at?: string | null
  } | null
}

export interface VideoRequestItem {
  id: string
  product: string
  description: string
  reference_url: string
  reference_video_key: string
  reference_video_name: string
  reference_video_url: string
  reference_videos: {
    object_key: string
    filename: string
    url: string
    original_url: string
    preview_url: string
    preview_status: 'pending' | 'processing' | 'ready' | 'failed' | string
    preview_error: string
  }[]
  reference_images: { object_key: string; filename: string; url: string }[]
  requester_number: string
  requester_name: string
  requester_department: string
  assignee_number: string
  assignee_name: string
  status: 'submitted' | 'assigned' | 'in_production' | 'delivered' | 'revision_requested' | 'accepted' | string
  status_label: string
  progress_percent: number
  progress_label: string
  latest_asset_id?: number | null
  latest_asset?: { id: number; filename: string; preview_url: string; download_url: string; cover_url: string } | null
  latest_assets: { id: number; filename: string; preview_url: string; download_url: string; cover_url: string; category: string; content_type: string; asset_subtype: string }[]
  delivery_version: number
  latest_feedback: string
  return_reason: string
  returned_by_number: string
  returned_by_name: string
  returned_at?: string | null
  assigned_at?: string | null
  started_at?: string | null
  delivered_at?: string | null
  accepted_at?: string | null
  created_at: string
  updated_at: string
  permissions: { can_assign: boolean; can_work: boolean; can_review: boolean; can_view_supervisor: boolean }
  deliveries: VideoRequestDelivery[]
  events: VideoRequestEvent[]
  metrics: VideoRequestMetrics
}

export interface VideoRequestPage {
  items: VideoRequestItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
  scope: 'mine' | 'assigned' | 'all'
}

export interface VideoRequestAssignee {
  number: string
  name: string
  department: string
}

export interface UserNotification {
  id: number
  kind: string
  title: string
  message: string
  resource_type: string
  resource_id: string
  external_status?: 'pending' | 'sending' | 'sent' | 'failed' | 'skipped'
  external_error?: string
  external_sent_at?: string | null
  read_at?: string | null
  created_at: string
}

export interface AdminGrant {
  identifier: string
  identifier_type: 'name' | 'number'
  real_name: string
  user_number: string
  department: string
  role: 'super_admin' | 'operation_admin'
  active: boolean
  protected: boolean
  granted_by_name: string
  granted_at: string
  revoked_by_name: string
  revoked_at?: string | null
}

export interface OperationLog {
  id: number
  actor_number: string
  actor_name: string
  department: string
  module: string
  action: string
  method: string
  path: string
  result: 'success' | 'failed'
  status_code: number
  resource_type: string
  resource_id: string
  detail: string
  created_at: string
}

export interface ChannelsAccount {
  id: string
  nickname: string
  avatar_url: string
  status: string
  message: string
  authorized_at?: string | null
  promotion: ChannelsPromotionAuth
}

export interface ChannelsPromotionAuth {
  authorized: boolean
  status: string
  message: string
  nickname: string
  user_type?: number | null
  account_type: string
  is_enterprise: boolean
  balance?: number | null
  authorized_at?: string | null
  expires_at?: string | null
}

export interface ChannelsStatus {
  configured: boolean
  message: string
  account_count: number
  capabilities: {
    qr_authorization: boolean
    original_video_publish: boolean
    batch_publish: boolean
    per_user_isolation: boolean
  }
  metrics: {
    mode: 'daily'
    timezone: string
    daily_hour: number
    last_completed_date?: string | null
    state: string
    processed: number
    errors: number
  }
}

export interface ChannelsAuthSession {
  id: string
  status: string
  message: string
  qr_ready: boolean
  capture_revision: number
  account_id: string
  interaction_required: boolean
  capture_mode: 'qr' | 'account_list' | 'account_choice'
  account_choices: { choice_id: string; label: string; detail: string }[]
  updated_at: string
}

export interface ChannelsPromotionAuthSession {
  id: string
  account_id: string
  status: 'starting' | 'waiting_scan' | 'authorized' | 'failed' | 'expired' | string
  message: string
  capture_ready: boolean
  capture_revision: number
  updated_at: string
}

export type ChannelsPromotionTarget =
  | 'product_click'
  | 'product_pay'
  | 'net_product_pay'
  | 'deal_roi'
  | 'net_deal_roi'
  | 'smart'
  | 'play'
  | 'like'
  | 'follow'
  | 'click'
  | 'heart'

export interface ChannelsPromotionConfiguration {
  funding_type: 'wecoin' | 'cash' | 'auto'
  bid_mode: 'volume' | 'cost_control'
  bid_value?: number | null
  start_mode: 'immediate' | 'scheduled'
  scheduled_at?: string | null
  billing_method: 'prepaid' | 'realtime'
  promotion_mode: 'smart' | 'targeted'
  portrait_mode: 'none' | 'authorized'
  voucher_mode: 'none' | 'max'
}

export interface ChannelsPromotionQuoteInput extends ChannelsPromotionConfiguration {
  promotion_target: ChannelsPromotionTarget
  budget_wecoin: number
  duration_hours: number
}

export interface ChannelsPromotionQuoteResult {
  ok: boolean
  task_id: string
  platform_export_id: string
  promotion_target: ChannelsPromotionTarget
  budget_wecoin: number
  duration_hours: number
  need_pay: number
  balance?: number | null
  quote_fallback: boolean
  nickname: string
  user_type?: number | null
  account_type?: string
  configuration?: ChannelsPromotionConfiguration
}

export interface ChannelsPromotionOrder {
  id: string
  delivery_id: string
  account_id: string
  promotion_target: ChannelsPromotionTarget
  budget_wecoin: number
  quoted_wecoin?: number | null
  duration_hours: number
  order_name: string
  status: 'submitting' | 'pending_payment' | 'payment_processing' | 'success' | 'failed' | 'manual_review' | 'cancelled' | string
  promotion_id: string
  cost_wecoin?: number | null
  error_code: string
  error_message: string
  created_at: string
  updated_at: string
  duplicate_request?: boolean
  platform_message?: string
}

export interface ChannelsPromotionPaymentSession {
  id: string
  order_id: string
  status: 'preparing' | 'awaiting_scan' | 'confirming' | 'succeeded' | 'expired' | 'cancelled' | 'failed' | 'uncertain' | string
  expected_amount: number
  sdk_status: string
  launch_url: string
  expires_at?: string | null
  completed_at?: string | null
  created_at: string
  updated_at: string
}

export interface ChannelsProduct {
  id: string
  name: string
  price_yuan?: number | null
  image_url?: string
}

export type ChannelsVideoAnnotationCode = 'none' | 'ai_generated' | 'fictional' | 'personal_opinion' | 'marketing_ad' | 'self_shot' | 'repost'

export interface ChannelsVideoAnnotationInput {
  asset_id: number
  annotation: ChannelsVideoAnnotationCode
  shooting_time: string
  shooting_location: string
  repost_source: string
}

export interface ChannelsTask {
  id: string
  batch_id: string
  asset_id: number
  asset_name: string
  created_by_number: string
  created_by_name: string
  can_manage: boolean
  can_edit?: boolean
  cover_filename?: string
  cover_status?: 'not_requested' | 'unverified' | 'verified' | 'mismatch'
  cover_message?: string
  cover_url?: string
  can_cancel: boolean
  account_id: string
  account_name: string
  title: string
  description: string
  tags: string[]
  product_id: string
  product_name: string
  video_annotation: ChannelsVideoAnnotationCode
  video_annotation_label: string
  annotation_shooting_time: string
  annotation_shooting_location: string
  annotation_repost_source: string
  status: 'pending' | 'publishing' | 'cancel_requested' | 'cancelled' | 'submitted' | 'success' | 'failed' | string
  message: string
  failure_stage: string
  attempt_count: number
  error_message: string
  platform_content_id: string
  platform_export_id: string
  platform_export_source: string
  platform_export_verified_at?: string | null
  platform_content_url: string
  can_promote: boolean
  latest_promotion?: ChannelsPromotionOrder | null
  view_count?: number | null
  like_count?: number | null
  comment_count?: number | null
  share_count?: number | null
  order_count?: number | null
  gmv_yuan?: number | null
  metrics_date?: string | null
  metrics_message: string
  metrics_updated_at?: string | null
  submitted_at?: string | null
  published_at?: string | null
  created_at: string
  updated_at: string
}

export interface OaAccessGrant {
  identifier: string
  identifier_type: 'name' | 'number'
  real_name: string
  user_number: string
  department: string
  active: boolean
  source: string
  granted_by_number: string
  granted_by_name: string
  granted_at: string
  revoked_by_name: string
  revoked_at?: string | null
  last_verified_at?: string | null
}

export interface OaAccessAudit {
  id: number
  identifier: string
  real_name: string
  action: 'grant' | 'revoke'
  actor_number: string
  actor_name: string
  detail: string
  created_at: string
}

export interface OaAccessGrantResult {
  items: OaAccessGrant[]
  total: number
  audits: OaAccessAudit[]
  department_policy: string
}

export interface PersonalSalesRanking {
  rank: number
  personName: string
  employeeId: string
  department: string
  qianchuanGmvYuan: number
  videoHaitunGmvYuan: number
  videoAdqGmvYuan: number
  videoGmvYuan: number
  totalGmvYuan: number
  materialCount: number
  qianchuanMaterialCount: number
  videoMaterialCount: number
}

export interface PersonalSalesSnapshot {
  schemaVersion: number
  status: 'ready' | 'partial'
  generatedAt: string
  meta: {
    periodStart: string
    periodEnd: string
    completeThroughDate: string
    qianchuanCutoffAt: string
    videoHaitunCutoffAt: string
    videoAdqCutoffDate: string
    schedule: string
    sourceLabel: string
    metricDefinition: {
      qianchuan: string
      videoHaitun: string
      videoAdq: string
    }
    peopleCount: number
    cappedQueries: string[]
    sourceFreshness?: Record<string, string>
  }
  quality: {
    totalSourceGmvYuan: number
    mappedGmvYuan: number
    unmappedGmvYuan: number
    conflictGmvYuan: number
    mappedGmvRate: number | null
    sourceMaterialCount: number
    mappedMaterialCount: number
    unmappedMaterialCount: number
    conflictMaterialCount: number
  }
  sourceTotals: {
    sourcePlatform: string
    channel: string
    gmvYuan: number
    materialRecordCount: number
  }[]
  rankings: PersonalSalesRanking[]
  automation: {
    configured: boolean
    schedule: string
    expectedCompleteThroughDate: string
    isFresh: boolean
    lastSuccessAt: string | null
    lastAttemptAt: string | null
    lastError: string
    preservesLastSuccessOnFailure: boolean
  }
}

export interface UploadAnalyticsDaily {
  date: string
  total: number
  source: number
  remix: number
  transacted: number
  hits: number
}

export interface UploadAnalyticsContributor {
  number: string
  name: string
  total: number
  source: number
  remix: number
  transacted: number
  hits: number
  daily: UploadAnalyticsDaily[]
}

export interface UploadAnalyticsResult {
  range: { start_date: string; end_date: string; days: number }
  summary: {
    total: number
    source: number
    remix: number
    transacted: number | null
    hits: number | null
  }
  daily: UploadAnalyticsDaily[]
  contributors: UploadAnalyticsContributor[]
  coverage: {
    state: 'available' | 'unavailable'
    message: string
    verified_rows: number
    metric_days: number
    latest_metric_at: string | null
    gmv_source: string
    upload_time_basis: string
  }
}

export interface PushScheme {
  id: string
  name: string
  targets: QianchuanTarget[]
  revision: number
  updated_at: string
}
