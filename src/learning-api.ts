import type { AttemptState } from './simulation';

export interface LearningCourse {
  id: string;
  title: string;
  description: string;
  equipment: string;
  source_label: string;
  release_id: string | null;
  release_version?: number | null;
  release_content_hash?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
  approval_note?: string | null;
  video_url: string;
  video_duration_seconds: number;
  video_configured: boolean;
  knowledge_units: Record<string, unknown>[];
  evidence_items?: { id: string; type: string; content: string | null; author: string; source_label: string; start_ms?: number | null; end_ms?: number | null }[];
}
export interface InterviewQuestion {
  id: string;
  prompt: string;
  source_context?: string;
  options: { id: string; label: string }[];
}
export interface LearningProgress {
  evaluation?: { playground_score: number | null; interview_score: number | null; overall_score: number | null; weighting: string; critical_playground_error: boolean };
  course_id: string;
  release_id: string | null;
  watched_seconds: number;
  course_completed: boolean;
  playground_unlocked: boolean;
  interview_unlocked: boolean;
  manual_completion?: { course?: string; playground?: string };
  playground_result: {
    id: string; scenario: string; mode: string;
    scores: { procedure: number; judgment: number; knowledge: number };
    outcome: string; critical_error: boolean; release_id: string | null;
  } | null;
  interview: {
    id: string; status: 'in_progress' | 'completed'; revision: number;
    source_release_id?: string;
    source_content_hash?: string;
    script_hash?: string;
    generation_method?: string;
    transcript?: { question: string; answer: string }[];
    duration_seconds?: number | null;
    current_question: InterviewQuestion | null;
    answers: { question_id: string; option_id: string; question_prompt?: string; selected_answer?: string; reflection: string; feedback: string; points: number; evidence_ids: string[] }[];
    result: { explanation_points: number; available_points: number; recognition_of_limits: number; critical_playground_error: boolean; playground_assessed?: boolean; notice: string; summary?: string; assessment_status?: string } | null;
  } | null;
}
export interface PlaybackSession { session_id: string; position_seconds: number; progress: LearningProgress }
export interface InterviewAnswer { session_id: string; question_id: string; option_id: string; reflection?: string; expected_revision: number }

export class LearningApiError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); this.name = 'LearningApiError'; }
}
interface Credentials { access_token: string; expires_at: string }
const TOKEN_KEY = 'takumi-learning-session';
let credentials: Credentials | null = null;
let loginRequest: Promise<Credentials> | null = null;

function readCredentials(): Credentials | null {
  if (credentials) return credentials;
  try {
    const stored = sessionStorage.getItem(TOKEN_KEY);
    if (stored) {
      const value: unknown = JSON.parse(stored);
      if (value && typeof value === 'object' && 'access_token' in value && 'expires_at' in value
        && typeof value.access_token === 'string' && typeof value.expires_at === 'string') credentials = value as Credentials;
    }
  } catch { /* Storage may be disabled; the in-memory session still works. */ }
  return credentials;
}
async function parseResponse<T>(response: Response): Promise<T> {
  let body: unknown;
  try { body = await response.json(); } catch { throw new LearningApiError('INVALID_RESPONSE', 'The learning service returned an unreadable response.', response.status); }
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } })?.error;
    throw new LearningApiError(error?.code ?? 'REQUEST_FAILED', error?.message ?? 'The learning service could not complete this request.', response.status);
  }
  return body as T;
}
async function authenticate(): Promise<Credentials> {
  const current = readCredentials();
  if (current && Date.parse(current.expires_at) > Date.now() + 30_000) return current;
  if (!loginRequest) loginRequest = (async () => {
    const response = await fetch('/api/v1/auth/demo-login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ demo_user: 'apprentice' }),
    });
    credentials = await parseResponse<Credentials>(response);
    try { sessionStorage.setItem(TOKEN_KEY, JSON.stringify(credentials)); } catch { /* Optional persistence. */ }
    return credentials;
  })().finally(() => { loginRequest = null; });
  return loginRequest;
}
async function request<T>(path: string, body?: unknown, retry = true): Promise<T> {
  try {
    const auth = await authenticate();
    const response = await fetch(`/api/v1/learning${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${auth.access_token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (response.status === 401 && retry) {
      credentials = null;
      try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* Optional persistence. */ }
      return request<T>(path, body, false);
    }
    return await parseResponse<T>(response);
  } catch (error) {
    if (error instanceof LearningApiError) throw error;
    throw new LearningApiError('SERVICE_UNAVAILABLE', 'The learning service is unavailable. Check that the backend is running and try again.', 0);
  }
}

export const learningApi = {
  getCourse: () => request<LearningCourse>('/course'),
  getProgress: () => request<LearningProgress>('/progress'),
  completeSection: (stage: 'course' | 'playground') => request<LearningProgress>('/complete', { stage }),
  startPlayback: () => request<PlaybackSession>('/playback', {}),
  heartbeat: (sessionId: string, positionSeconds: number) => request<LearningProgress>(`/playback/${encodeURIComponent(sessionId)}`, { position_seconds: positionSeconds }),
  submitPlayground: (attempt: AttemptState) => request<LearningProgress>('/playground', {
    scenario: attempt.scenarioId, mode: attempt.mode, actions: attempt.log.map(entry => entry.actionId),
  }),
  startInterview: () => request<LearningProgress>('/interview', {}),
  answerInterview: (answer: InterviewAnswer) => request<LearningProgress>('/interview/answers', answer),
  resetKnowledge: () => request<LearningProgress>('/reset', {}),
  getToken: async (): Promise<string> => {
    const auth = await authenticate();
    return auth.access_token;
  },
};

