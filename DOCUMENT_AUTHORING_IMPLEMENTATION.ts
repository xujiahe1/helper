// ============================================================================
// 1. 数据模型 (types/authoring.ts)
// ============================================================================

export interface DocumentAuthoring {
  sessionId: string;
  templateId: string;
  documentInfo: {
    title: string;
    knowledgeId?: string;
    parentDocId?: string;
    ownerId?: string;
    targetDocId?: string;
  };
  writingPlan: {
    outline: string[];
    constraints: string[];
    confirmedAt: number;
  };
  phases: PhaseRuntime[];
  currentPhaseIndex: number;
  fullDraft: {
    markdown: string;
    lastUpdated: number;
    version: number;
  };
  startedAt: number;
  lastModified: number;
  status: 'planning' | 'in-progress' | 'paused' | 'completed' | 'failed';
}

export interface PhaseRuntime {
  phaseId: string;
  phaseName: string;
  order: number;
  status: 'pending' | 'in-progress' | 'confirmed' | 'skipped';
  collectedInfo: Record<string, string | string[]>;
  conversationHistory: PhaseMessage[];
  draft: {
    markdown: string;
    startLine: number;
    lineCount: number;
  };
  summary?: PhaseSummary;
}

export interface PhaseSummary {
  phaseName: string;
  keyPoints: string[];
  decisions: string[];
  draftSize: number;
  completedAt: number;
}

export interface PhaseMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  type: 'question' | 'answer' | 'draft' | 'confirmation';
}

// ============================================================================
// 2. 撰写引擎 Service (services/documentAuthoringService.ts)
// ============================================================================

import { DocumentAuthoring, PhaseRuntime, PhaseMessage } from '@/types/authoring';

export class DocumentAuthoringEngine {
  // 创建新会话
  createSession(templateId: string, initialInfo: any): DocumentAuthoring {
    const template = this.templateService.getTemplate(templateId);
    if (!template) throw new Error('Template not found');

    return {
      sessionId: generateId(),
      templateId,
      documentInfo: initialInfo,
      writingPlan: {
        outline: [],
        constraints: [],
        confirmedAt: 0,
      },
      phases: template.phases.map((pt, idx) => ({
        phaseId: pt.id,
        phaseName: pt.name,
        order: pt.order,
        status: 'pending',
        collectedInfo: {},
        conversationHistory: [],
        draft: {
          markdown: '',
          startLine: 0,
          lineCount: 0,
        },
      })),
      currentPhaseIndex: 0,
      fullDraft: {
        markdown: '',
        lastUpdated: Date.now(),
        version: 0,
      },
      startedAt: Date.now(),
      lastModified: Date.now(),
      status: 'planning',
    };
  }

  // 获取当前阶段
  getCurrentPhase(session: DocumentAuthoring): PhaseRuntime {
    return session.phases[session.currentPhaseIndex];
  }

  // 记录答案
  recordAnswer(
    session: DocumentAuthoring,
    questionId: string,
    answer: string | string[]
  ): void {
    const phase = this.getCurrentPhase(session);
    phase.collectedInfo[questionId] = answer;
    session.lastModified = Date.now();
  }

  // 生成本阶段草稿
  async generatePhaseDraft(session: DocumentAuthoring): Promise<string> {
    const phase = this.getCurrentPhase(session);
    const template = this.templateService.getTemplate(session.templateId);
    const phaseTemplate = template.phases.find(p => p.id === phase.phaseId);

    // 构建 prompt
    const context = this.buildGenerationContext(session);
    const answers = Object.entries(phase.collectedInfo)
      .map(([qId, ans]) => {
        const q = phaseTemplate.questions.find(qq => qq.id === qId);
        return `${q.text}\n${ans}`;
      })
      .join('\n\n');

    const prompt = `
基于用户的回答，为 "${phase.phaseName}" 章节生成 Markdown 内容。

要求:
- 遵循 Markdown 语法
- 结构清晰，层次分明
- ${phaseTemplate.outputHint}
- 约束: ${session.writingPlan.constraints.join('; ')}

用户回答:
${answers}

请生成该章节的内容（仅包含该章节，不需要标题）:
`;

    const response = await this.llmService.streamChat({
      messages: [
        {
          role: 'system',
          content: '你是专业文档撰写助手。基于用户信息生成结构清晰的 Markdown 内容。',
        },
        {
          role: 'user',
          content: prompt,
        },
      ],
      model: this.config.models.system,
      maxTokens: 4000,
    });

    phase.draft.markdown = response;
    session.lastModified = Date.now();

    return response;
  }

  // 确认阶段
  async confirmPhase(session: DocumentAuthoring, feedback?: string): Promise<void> {
    const phase = this.getCurrentPhase(session);

    // 生成摘要
    phase.summary = await this.generatePhaseSummary(phase);
    phase.status = 'confirmed';

    // 清理对话历史
    phase.conversationHistory = [
      phase.conversationHistory[phase.conversationHistory.length - 1],
    ];

    session.lastModified = Date.now();
  }

  // 推进到下一阶段
  advancePhase(session: DocumentAuthoring): void {
    if (session.currentPhaseIndex < session.phases.length - 1) {
      session.currentPhaseIndex++;
      session.phases[session.currentPhaseIndex].status = 'in-progress';
    } else {
      session.status = 'completed';
    }
    session.lastModified = Date.now();
  }

  // 完成撰写
  finalizeDocument(session: DocumentAuthoring): {
    markdown: string;
    estimatedTokens: number;
  } {
    // 合并所有阶段的草稿
    const fullMarkdown = session.phases
      .filter(p => p.status === 'confirmed')
      .map(p => {
        const template = this.templateService.getTemplate(session.templateId);
        const pt = template.phases.find(x => x.id === p.phaseId);
        return `## ${pt.name}\n\n${p.draft.markdown}`;
      })
      .join('\n\n');

    // 估算 token
    const estimatedTokens = Math.ceil(fullMarkdown.length / 4);

    session.fullDraft.markdown = fullMarkdown;
    session.fullDraft.version++;

    return {
      markdown: fullMarkdown,
      estimatedTokens,
    };
  }

  // 私有方法
  private buildGenerationContext(session: DocumentAuthoring): string {
    // 构建生成上下文（包含已完成阶段的信息）
    return '';
  }

  private async generatePhaseSummary(phase: PhaseRuntime): Promise<PhaseSummary> {
    // 用 LLM 生成摘要
    return {
      phaseName: phase.phaseName,
      keyPoints: [],
      decisions: [],
      draftSize: phase.draft.markdown.length,
      completedAt: Date.now(),
    };
  }
}

// ============================================================================
// 3. Context 构建 (services/contextBuilder.ts)
// ============================================================================

export class AuthoringContextBuilder {
  async buildContext(
    session: DocumentAuthoring,
    userInput: string,
    prdStore: PrdStore
  ): Promise<string> {
    const parts: string[] = [];

    // L0: 系统层
    parts.push(this.buildSystemLayer());

    // L1: 文档计划层
    parts.push(this.buildPlanLayer(session));

    // L2: 进度层
    parts.push(this.buildProgressLayer(session));

    // L3: 工作记忆层
    const currentPhase = session.phases[session.currentPhaseIndex];
    parts.push(this.buildWorkingMemoryLayer(currentPhase));

    // L4: 知识层（按需）
    const relatedEntities = await prdStore.matchEntities(userInput);
    if (relatedEntities.length > 0) {
      parts.push(this.buildKnowledgeLayer(relatedEntities));
    }

    return parts.filter(p => p.trim()).join('\n\n---\n\n');
  }

  private buildSystemLayer(): string {
    return `你是专业文档撰写助手，遵循以下原则：
- 生成高质量的 Markdown 文档
- 基于用户回答补充和完善内容
- 保持逻辑清晰、结构合理
- 避免冗余和重复`;
  }

  private buildPlanLayer(session: DocumentAuthoring): string {
    return `## 文档计划

模板: ${session.documentInfo.title}
预期长度: ${session.writingPlan.constraints.join('; ')}

阶段:
${session.phases
  .map(
    (p, i) =>
      `${i + 1}. ${p.phaseName} ${
        p.status === 'confirmed' ? '✅' : p.status === 'in-progress' ? '🔄' : '⚪'
      }`
  )
  .join('\n')}`;
  }

  private buildProgressLayer(session: DocumentAuthoring): string {
    const completed = session.phases
      .filter(p => p.status === 'confirmed')
      .map(p => `- **${p.phaseName}**: ${p.summary?.keyPoints.join('; ')}`);

    if (completed.length === 0) return '';

    return `## 已完成阶段\n\n${completed.join('\n')}`;
  }

  private buildWorkingMemoryLayer(phase: PhaseRuntime): string {
    const info = Object.entries(phase.collectedInfo)
      .map(([qId, ans]) => `- Q${qId}: ${ans}`)
      .join('\n');

    return `## 当前阶段: ${phase.phaseName}

已收集信息:
${info}`;
  }

  private buildKnowledgeLayer(entities: any[]): string {
    return `## 相关知识

${entities
  .slice(0, 5)
  .map(e => `- **${e.name}**: ${e.description.slice(0, 150)}...`)
  .join('\n')}`;
  }
}

// ============================================================================
// 4. KM 写入 Service (services/kmWriteService.ts)
// ============================================================================

export class KmWriteManager {
  async createAndWrite(params: {
    markdown: string;
    title: string;
    knowledgeId: string;
    ownerId: string;
    parentDocId?: string;
  }): Promise<WriteResult> {
    const startTime = Date.now();

    try {
      // 1. 创建文档
      const createResp = await this.mcpClient.call('create_document', {
        knowledge_id: params.knowledgeId,
        title: params.title,
        owner_id: params.ownerId,
        parent_doc_id: params.parentDocId,
      });

      const docId = createResp.data.doc_id;
      const docUrl = createResp.data.doc_url;

      // 2. 转换 + 分块
      const kmJson = await this.convertMarkdownToKmJson(params.markdown);
      const chunks = this.splitByHeading(kmJson, { maxChunkSize: 30000 });

      // 3. 写入
      let successCount = 0;
      const failedChunks: any[] = [];

      for (let i = 0; i < chunks.length; i++) {
        try {
          const chunk = chunks[i];
          const anchor = i === 0 ? '1' : 'last';
          const action = i === 0 ? 'replace' : 'insert_after';

          await this.mcpClient.call('edit_document', {
            doc_id: docId,
            anchor,
            action,
            content: chunk.blocks,
          });

          successCount++;
          await this.delay(500);
        } catch (error) {
          failedChunks.push({
            index: i,
            error: error.message,
            retryable: true,
          });
        }
      }

      return {
        status: failedChunks.length === 0 ? 'success' : 'partial',
        docId,
        url: docUrl,
        failedChunks: failedChunks.length > 0 ? failedChunks : undefined,
        stats: {
          totalChunks: chunks.length,
          successChunks: successCount,
          timeMs: Date.now() - startTime,
        },
      };
    } catch (error) {
      return {
        status: 'failed',
        docId: '',
        url: '',
        failedChunks: [{ index: 0, error: error.message, retryable: true }],
        stats: {
          totalChunks: 0,
          successChunks: 0,
          timeMs: Date.now() - startTime,
        },
      };
    }
  }

  private async convertMarkdownToKmJson(markdown: string): Promise<any> {
    // 使用 markdown-it 解析 Markdown
    // 返回 KM JSON 结构
    return {};
  }

  private splitByHeading(kmDoc: any, opts: any): any[] {
    // 按一级标题分块
    return [];
  }

  private delay(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// ============================================================================
// 5. React Store (stores/authoringStore.ts)
// ============================================================================

import create from 'zustand';
import { DocumentAuthoring } from '@/types/authoring';

interface AuthoringStore {
  sessions: Map<string, DocumentAuthoring>;
  currentSessionId: string | null;

  // 会话管理
  createSession: (templateId: string, info: any) => string;
  getSession: (sessionId: string) => DocumentAuthoring | null;
  setCurrentSession: (sessionId: string) => void;

  // 更新会话
  updateCurrentSession: (updates: Partial<DocumentAuthoring>) => void;

  // 暂停/恢复
  pauseSession: (sessionId: string) => void;
  resumeSession: (sessionId: string) => void;

  // 删除
  deleteSession: (sessionId: string) => void;
}

export const useAuthoringStore = create<AuthoringStore>((set, get) => ({
  sessions: new Map(),
  currentSessionId: null,

  createSession: (templateId, info) => {
    const engine = new DocumentAuthoringEngine();
    const session = engine.createSession(templateId, info);
    const newSessions = new Map(get().sessions);
    newSessions.set(session.sessionId, session);
    set({ sessions: newSessions, currentSessionId: session.sessionId });
    return session.sessionId;
  },

  getSession: (sessionId) => {
    return get().sessions.get(sessionId) || null;
  },

  setCurrentSession: (sessionId) => {
    set({ currentSessionId: sessionId });
  },

  updateCurrentSession: (updates) => {
    const sessionId = get().currentSessionId;
    if (!sessionId) return;

    const session = get().sessions.get(sessionId);
    if (!session) return;

    const newSessions = new Map(get().sessions);
    newSessions.set(sessionId, { ...session, ...updates });
    set({ sessions: newSessions });
  },

  pauseSession: (sessionId) => {
    const session = get().sessions.get(sessionId);
    if (session) {
      session.status = 'paused';
      set({ sessions: new Map(get().sessions) });
    }
  },

  resumeSession: (sessionId) => {
    const session = get().sessions.get(sessionId);
    if (session) {
      session.status = 'in-progress';
      set({ sessions: new Map(get().sessions) });
    }
  },

  deleteSession: (sessionId) => {
    const newSessions = new Map(get().sessions);
    newSessions.delete(sessionId);
    set({ sessions: newSessions });
  },
}));

// ============================================================================
// 6. React 组件 - 主面板 (components/DocumentAuthoringPanel.tsx)
// ============================================================================

import React, { useState } from 'react';
import { useAuthoringStore } from '@/stores/authoringStore';
import { QuestionnaireCard } from './QuestionnaireCard';
import { RightPanel } from './RightPanel';

export const DocumentAuthoringPanel: React.FC = () => {
  const { sessions, currentSessionId, setCurrentSession } = useAuthoringStore();
  const session = currentSessionId ? sessions.get(currentSessionId) : null;

  if (!session) return null;

  return (
    <div className="flex gap-4">
      {/* 左侧: 聊天区 */}
      <div className="flex-1">
        <QuestionnaireCard session={session} />
      </div>

      {/* 右侧: 文档预览 */}
      <div className="w-1/3">
        <RightPanel session={session} />
      </div>
    </div>
  );
};

export default DocumentAuthoringPanel;

// ============================================================================
// 7. React 组件 - 问卷卡片 (components/QuestionnaireCard.tsx)
// ============================================================================

import React, { useState } from 'react';
import { DocumentAuthoring, PhaseRuntime } from '@/types/authoring';
import { useAuthoringStore } from '@/stores/authoringStore';

interface QuestionnaireCardProps {
  session: DocumentAuthoring;
}

export const QuestionnaireCard: React.FC<QuestionnaireCardProps> = ({ session }) => {
  const { updateCurrentSession } = useAuthoringStore();
  const [isGenerating, setIsGenerating] = useState(false);

  const phase = session.phases[session.currentPhaseIndex];
  const template = getTemplate(session.templateId);
  const phaseTemplate = template.phases.find(p => p.id === phase.phaseId);

  const handleAnswer = (questionId: string, answer: any) => {
    const updated = { ...session };
    updated.phases[session.currentPhaseIndex].collectedInfo[questionId] = answer;
    updateCurrentSession(updated);
  };

  const handleGenerate = async () => {
    setIsGenerating(true);
    try {
      const engine = new DocumentAuthoringEngine();
      const draft = await engine.generatePhaseDraft(session);
      const updated = { ...session };
      updated.phases[session.currentPhaseIndex].draft.markdown = draft;
      updateCurrentSession(updated);
    } finally {
      setIsGenerating(false);
    }
  };

  const handleConfirm = async () => {
    const engine = new DocumentAuthoringEngine();
    const updated = { ...session };
    await engine.confirmPhase(updated);
    engine.advancePhase(updated);
    updateCurrentSession(updated);
  };

  return (
    <div className="card">
      <div className="card-header">
        <h3>📝 {phaseTemplate.name}</h3>
        <p>
          阶段 {session.currentPhaseIndex + 1}/{session.phases.length}
        </p>
      </div>

      <div className="card-body space-y-4">
        {phaseTemplate.questions.map((q, idx) => (
          <div key={q.id}>
            <label>
              {idx + 1}. {q.text}
              {q.required && <span className="text-red-500"> *</span>}
            </label>

            {q.type === 'text' && (
              <textarea
                placeholder={q.placeholder}
                rows={q.rows || 3}
                value={phase.collectedInfo[q.id] || ''}
                onChange={e => handleAnswer(q.id, e.target.value)}
                className="input"
              />
            )}

            {q.type === 'choice' && (
              <div className="space-y-2">
                {q.options?.map(opt => (
                  <label key={opt.label} className="flex items-center gap-2">
                    <input
                      type="radio"
                      name={q.id}
                      value={opt.label}
                      checked={phase.collectedInfo[q.id] === opt.label}
                      onChange={e => handleAnswer(q.id, e.target.value)}
                    />
                    {opt.label}
                  </label>
                ))}
              </div>
            )}

            {q.type === 'multi-choice' && (
              <div className="space-y-2">
                {q.options?.map(opt => (
                  <label key={opt.label} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={(phase.collectedInfo[q.id] as string[])?.includes(
                        opt.label
                      )}
                      onChange={e => {
                        const arr = (phase.collectedInfo[q.id] as string[]) || [];
                        if (e.target.checked) {
                          handleAnswer(q.id, [...arr, opt.label]);
                        } else {
                          handleAnswer(q.id, arr.filter(x => x !== opt.label));
                        }
                      }}
                    />
                    {opt.label}
                  </label>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="card-footer flex gap-2">
        <button onClick={handleGenerate} disabled={isGenerating} className="btn btn-primary">
          {isGenerating ? '生成中...' : '生成草稿'}
        </button>
        <button onClick={handleConfirm} className="btn btn-secondary">
          确认并继续
        </button>
        <button className="btn btn-text">跳过</button>
      </div>
    </div>
  );
};

// ============================================================================
// 8. 持久化 (utils/authoringStorage.ts)
// ============================================================================

export class AuthoringStorage {
  static saveSession(session: DocumentAuthoring): void {
    const key = `authoring:${session.sessionId}`;
    localStorage.setItem(key, JSON.stringify(session));

    // 更新索引
    const sessions = this.getSessions();
    if (!sessions.find(s => s.id === session.sessionId)) {
      sessions.push({
        id: session.sessionId,
        templateId: session.templateId,
        title: session.documentInfo.title,
        status: session.status,
        startedAt: session.startedAt,
      });
      localStorage.setItem('authoring:sessions', JSON.stringify(sessions));
    }
  }

  static loadSession(sessionId: string): DocumentAuthoring | null {
    const key = `authoring:${sessionId}`;
    const data = localStorage.getItem(key);
    return data ? JSON.parse(data) : null;
  }

  static getSessions(): any[] {
    const data = localStorage.getItem('authoring:sessions');
    return data ? JSON.parse(data) : [];
  }

  static deleteSession(sessionId: string): void {
    localStorage.removeItem(`authoring:${sessionId}`);
    const sessions = this.getSessions().filter(s => s.id !== sessionId);
    localStorage.setItem('authoring:sessions', JSON.stringify(sessions));
  }
}
