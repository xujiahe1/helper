// ============================================================
// 引导式 PRD 对话 — 核心类型定义
// ============================================================

/** 引导会话阶段（现在主要用于右侧面板展示，不再驱动流程） */
export type GuidedPhase =
  | 'active'     // 对话进行中（AI 自主驱动）
  | 'completed'  // 已完成（所有功能点锁定 + 写入完成）

/** 单个功能点的生命周期 */
export type FeatureStatus =
  | 'pending'    // 识别到但未开始深挖
  | 'drilling'   // 正在深挖
  | 'locked'     // 已锁定，有压缩摘要

/** 已锁定功能点 —— 压缩摘要，进入固定层 context */
export interface LockedFeature {
  featureId: string
  title: string
  /** ~150 字结构化摘要，每轮都注入 context */
  summary: string
  /** 写入文档后填充的锚点，用于后续更新 */
  anchor?: string
  lockedAt: number
}

/** 功能点来源 */
export type FeatureSource = 'ai' | 'user' | 'doc'  // AI 生成 | 用户手动添加 | 从文档导入

/** 功能点 */
export interface FeatureItem {
  featureId: string
  title: string
  status: FeatureStatus
  /** 深挖轮次数（用于右侧面板展示） */
  roundCount: number
  locked?: LockedFeature
  /** 用户可编辑的摘要（规划阶段由 AI 生成，用户可修改） */
  outline?: string
  /** AI 生成的实际内容（写入文档后保存） */
  generatedContent?: string
  /** 摘要是否被用户手动修改过 */
  userEdited?: boolean
  /** 模块来源 */
  source?: FeatureSource
  /** 文档中的锚点（从文档导入时记录，用于定位更新） */
  docAnchor?: string
  /** 文档中的原始内容摘要（用于判断是否需要更新） */
  originalContent?: string
}

// ============================================================
// 写入目标
// ============================================================

export type WriteTargetMode = 'new_doc' | 'existing_doc' | 'chat_only'

/** 写入目标 —— AI 推断后固化，进入固定层永不压缩 */
export interface WriteTarget {
  /** 人类可读描述，如"更新《消息通知PRD》第3章" */
  description: string
  mode: WriteTargetMode
  docId?: string
  docTitle?: string
  confirmedAt: number
  /** 历史目标记录（用于迁移判断） */
  previousTargets: Array<{
    description: string
    docId?: string
    docTitle?: string
    changedAt: number
  }>
}

/** 已写入章节记录 —— 目标变更时用于迁移 */
export interface WrittenSection {
  docId: string
  anchor: string
  heading: string
  content: string   // 保留内容副本，迁移时直接用
  writtenAt: number
}

/** 引导会话主状态 */
export interface GuidedSession {
  sessionId: string         // = conversationId
  phase: GuidedPhase
  features: FeatureItem[]
  /** PRD 草稿全文，仅右侧面板显示，永不进入 LLM context */
  prdDraft: string
  /** 当前对话使用的模型 */
  model: string
  /** 写入目标（AI 推断后固化，进入固定层） */
  writeTarget?: WriteTarget
  /** 已写入章节记录 */
  writtenSections: WrittenSection[]
  /**
   * 中途接管标记：记录 initSession 时对话已有的消息数。
   * > 0 表示这是在已有对话上开启的引导模式，需要在 system prompt 里加接管说明。
   * = 0 表示从空白对话启动，无需特殊处理。
   */
  resumedFromMessageCount: number
  /** 待重新生成的功能点 ID（用户修改摘要后设置，下次 sendMessage 时使用并清除） */
  pendingRegenerateFeatureId?: string
  createdAt: number
  updatedAt: number
}

// ============================================================
// Agent Action — AI 输出的结构化操作指令
// 渲染在 ```agent-action 代码块中，前端解析后执行
// ============================================================

/** 追问用户（渲染 FeatureDraftCard） */
export interface AskAction {
  type: 'ask'
  featureId: string
  featureTitle: string
  /** AI 当前对该功能点的理解草稿（折叠展示） */
  draft: string
  /** 需要用户澄清的问题（1-3 个） */
  questions: string[]
}

/** 请用户确认功能点内容后锁定 */
export interface ConfirmFeatureAction {
  type: 'confirm_feature'
  featureId: string
  featureTitle: string
  /** 完整的功能点描述，供用户确认 */
  summary: string
}

/** 直接锁定功能点（信息充分，无需用户确认） */
export interface LockFeatureAction {
  type: 'lock_feature'
  featureId: string
  featureTitle: string
  /** 压缩后的功能摘要（~150字），进入固定层 context */
  summary: string
}

/** 调 MCP 写入文档章节 */
export interface WriteDocAction {
  type: 'write_doc'
  /** 目标文档 ID（已有文档）或 null（需先创建） */
  docId: string | null
  /** 新建文档时的标题 */
  docTitle?: string
  /** 要写入的章节列表 */
  sections: Array<{
    /** 章节标题（用于锚点定位或新建时的标题） */
    heading: string
    /** Markdown 内容 */
    content: string
    /** 已有文档的锚点（更新时用），无则追加 */
    anchor?: string
  }>
}

/** 更新右侧面板草稿预览 */
export interface UpdateDraftAction {
  type: 'update_draft'
  content: string
}

/** 识别新功能点，注册到进度追踪 */
export interface RegisterFeatureAction {
  type: 'register_feature'
  features: Array<{
    featureId: string
    title: string
    /** 该功能点的内容摘要（用户可编辑） */
    outline?: string
  }>
}

/** AI 判断用户意图是写 PRD，主动触发引导模式 */
export interface StartGuidedAction {
  type: 'start_guided'
  /** AI 识别到的写作意图描述（给用户看的） */
  reason: string
}

/** AI 推断出写入目标后固化 */
export interface SetWriteTargetAction {
  type: 'set_write_target'
  description: string
  mode: WriteTargetMode
  docId?: string
  docTitle?: string
}

/** AI 从文档导入目录结构 */
export interface ImportDocStructureAction {
  type: 'import_doc_structure'
  sections: Array<{
    title: string
    anchor: string
    contentPreview?: string
  }>
}

export type AgentAction =
  | AskAction
  | ConfirmFeatureAction
  | LockFeatureAction
  | WriteDocAction
  | UpdateDraftAction
  | RegisterFeatureAction
  | SetWriteTargetAction
  | StartGuidedAction
  | ImportDocStructureAction

// ============================================================
// Message 卡片数据类型（用于渲染交互式卡片）
// ============================================================

export interface FeatureDraftCardData {
  type: 'feature_draft'
  featureId: string
  featureTitle: string
  /** 唯一标识本卡片，用于 submitAnswer 关联 */
  cardId: string
  draft: string
  questions: string[]
  submitted: boolean
}

export interface FeatureConfirmCardData {
  type: 'feature_confirm'
  featureId: string
  featureTitle: string
  cardId: string
  summary: string
  confirmed: boolean
}

export interface FeatureLockedCardData {
  type: 'feature_locked'
  featureId: string
  featureTitle: string
  summary: string
}

export interface WriteProgressCardData {
  type: 'write_progress'
  docId?: string
  docTitle?: string
  sections: Array<{
    heading: string
    status: 'pending' | 'writing' | 'done' | 'failed'
    anchor?: string
  }>
}

/** 写入目标变更确认卡片 */
export interface WriteTargetChangeCardData {
  type: 'write_target_change'
  cardId: string
  /** 旧目标描述 */
  oldTarget: {
    description: string
    docId?: string
    docTitle?: string
  }
  /** 新目标描述 */
  newTarget: {
    description: string
    docId?: string
    docTitle?: string
    mode: WriteTargetMode
  }
  /** 旧文档中已写入的章节数 */
  writtenSectionCount: number
  /** 新文档中检测到的已有章节（与待写章节重名的） */
  conflictingHeadings: string[]
  /** 用户选择 */
  migrateChoice: 'migrate' | 'keep_old' | null
  conflictChoice: 'overwrite' | 'append' | 'skip' | null
  resolved: boolean
}

export type PrdCardData =
  | FeatureDraftCardData
  | FeatureConfirmCardData
  | FeatureLockedCardData
  | WriteProgressCardData
  | WriteTargetChangeCardData
