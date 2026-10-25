import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, ArrowRight, BookOpen, Box, Check, ChevronRight, ClipboardCheck, LayoutDashboard, LockKeyhole, MessageSquare, Mic, RefreshCw, RotateCcw, ShieldCheck } from 'lucide-react';
import Playground from '../App';
import type { AttemptState } from '../simulation';
import { learningApi } from '../learning-api';
import type { LearningCourse, LearningProgress } from '../learning-api';
import { CoursePlayer } from './CoursePlayer';
import VirtualInterview from './VirtualInterview';
import FinalEvaluation from './FinalEvaluation';
import { STORAGE_KEY } from '../persistence';
import { createLearningReport } from '../learning-report';
import takumiLogo from '../assets/Takumi_Horizontal_Transparent_White.png';
import jetroLogo from '../assets/jetro-logo-png_seeklogo-75464.png';
import '../course.css';
import '../virtual-interview.css';

function courseRecordingIdentity(value: string) {
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin === window.location.origin && /^\/api\/v1\/media\/[^/]+\/stream$/.test(url.pathname)) {
      // Signed playback tokens rotate even when the recording has not changed.
      url.searchParams.delete('token');
    }
    return url.href;
  } catch { return value; }
}

// Only same-origin media streams ignore rotating playback tokens.
function courseRecordingKey(value: string) {
  const recording = courseRecordingIdentity(value);
  let hash = 0;
  for (let index = 0; index < recording.length; index++) hash = ((hash << 5) - hash + recording.charCodeAt(index)) | 0;
  return hash.toString(36);
}

type Tab = 'dashboard' | 'course' | 'playground' | 'interview';
const tabs = [
  { id: 'dashboard' as const, label: 'Dashboard', icon: LayoutDashboard },
  { id: 'course' as const, label: 'Course content', icon: BookOpen },
  { id: 'playground' as const, label: 'Simulation playground', icon: Box },
  { id: 'interview' as const, label: 'Virtual interview', icon: MessageSquare },
];

export default function CourseHub() {
  const [tab, setTab] = useState<Tab>('dashboard');
  const [course, setCourse] = useState<LearningCourse | null>(null);
  const [progress, setProgress] = useState<LearningProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionCount, setActionCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [showResetConfirm, setShowResetConfirm] = useState(false);
  const [resetEpoch, setResetEpoch] = useState(0);
  const [message, storeMessage] = useState('Connecting to your learning record…');
  const [notification, setNotification] = useState<{ text: string; isError: boolean } | null>(null);
  const setMessage = useCallback((text: string, isError = false) => {
    storeMessage(text); setNotification({ text, isError });
  }, []);
  useEffect(() => {
    if (!notification || notification.isError) return;
    const timer = window.setTimeout(() => setNotification(null), 5000);
    return () => window.clearTimeout(timer);
  }, [notification]);
  const [failedAttempt, setFailedAttempt] = useState<AttemptState | null>(null);
  const [selectedAnswer, setSelectedAnswer] = useState('');
  const [reflection, setReflection] = useState('');
  const [interviewMode, setInterviewMode] = useState<'choice' | 'structured' | 'live'>('choice');
  const [liveToken, setLiveToken] = useState('');

  const initialize = useCallback(async () => {
    setLoading(true);
    try {
      const [nextCourse, nextProgress] = await Promise.all([learningApi.getCourse(), learningApi.getProgress()]);
      setCourse(nextCourse); setProgress(nextProgress);
      setMessage('Learning record connected. Complete each stage to continue.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Unable to connect to the learning service.', true); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void initialize(); }, [initialize]);

  const checkCourseUpdates = useCallback(async () => {
    try {
      const nextCourse = await learningApi.getCourse();
      if (course && (nextCourse.release_id !== course.release_id || nextCourse.release_content_hash !== course.release_content_hash || courseRecordingIdentity(nextCourse.video_url) !== courseRecordingIdentity(course.video_url) || nextCourse.video_duration_seconds !== course.video_duration_seconds)) {
        const nextProgress = await learningApi.getProgress();
        setCourse(nextCourse); setProgress(nextProgress); setTab('course');
        setSelectedAnswer(''); setReflection(''); setFailedAttempt(null);
        setMessage('A newly approved course release is available. Review the updated course before continuing.');
      } else {
        // Keep fresh credentials for the next visit without navigating or resetting progress.
        setCourse(nextCourse);
      }
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Unable to check course updates.', true); }
  }, [course, setMessage]);
  useEffect(() => {
    const refresh = () => { void checkCourseUpdates(); };
    const publication = (event: StorageEvent) => { if (event.key === 'takumi-publication') refresh(); };
    const visibleRefresh = () => { if (document.visibilityState === 'visible') refresh(); };
    const interval = window.setInterval(visibleRefresh, 10_000);
    window.addEventListener('focus', refresh);
    window.addEventListener('storage', publication);
    return () => { window.removeEventListener('focus', refresh); window.removeEventListener('storage', publication); window.clearInterval(interval); };
  }, [checkCourseUpdates]);

  function downloadReport() {
    if (!course || !progress) return;
    const url = URL.createObjectURL(new Blob([createLearningReport(course, progress)], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = 'takumi-learning-report.md'; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const updateProgress = useCallback((next: LearningProgress) => setProgress(next), []);
  const recordAttempt = useCallback(async (attempt: AttemptState) => {
    setBusy(true); setFailedAttempt(attempt);
    try {
      const next = await learningApi.submitPlayground(attempt);
      setProgress(next); setFailedAttempt(null);
      setMessage('Playground result saved. Your virtual interview is available.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Your result could not be saved. Retry below.', true); }
    finally { setBusy(false); }
  }, []);

  const interview = progress?.interview;
  const question = interview?.current_question;
  const playgroundCompleted = Boolean(progress?.playground_result || progress?.manual_completion?.playground);
  const completed = Number(!!progress?.course_completed) + Number(playgroundCompleted) + Number(interview?.status === 'completed');
  const nextTab: Tab = !progress?.course_completed ? 'course' : !playgroundCompleted ? 'playground' : 'interview';
  const goNext = () => setTab(nextTab);
  const locked = (id: Tab) => id === 'playground' ? !progress?.playground_unlocked : id === 'interview' ? !progress?.interview_unlocked : false;
  const stageStatus = (index: number) => index === 0 ? progress?.manual_completion?.course ? 'Manually completed' : progress?.course_completed ? 'Completed' : 'Current stage' : index === 1 ? progress?.manual_completion?.playground ? 'Manually completed' : progress?.playground_result ? 'Completed' : progress?.playground_unlocked ? 'Ready to start' : 'Complete the course first' : interview?.status === 'completed' ? 'Completed' : progress?.interview_unlocked ? 'Ready to start' : 'Complete the playground first';

  async function completeSection(stage: 'course' | 'playground') {
    if (!course?.release_id) {
      setMessage('A reviewed course release must be published before completing training sections. Please publish in Expert Studio.', true);
      return;
    }
    setBusy(true);
    try {
      setProgress(await learningApi.completeSection(stage));
      setMessage(stage === 'course' ? 'Course section manually completed. The simulation playground is available.' : 'Playground section manually completed. No simulation score was recorded. Your virtual interview is available.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Section completion could not be saved. Please retry.', true); }
    finally { setBusy(false); }
  }

  async function startInterview() {
    setBusy(true);
    try { setProgress(await learningApi.startInterview()); setMessage(progress?.playground_result ? 'Interview started. Use the course evidence and your playground result to answer.' : 'Interview started. The playground was manually completed; no simulation score has been recorded.'); }
    catch (error) { setMessage(error instanceof Error ? error.message : 'Could not start the interview.', true); }
    finally { setBusy(false); }
  }
  async function submitAnswer() {
    if (!interview || !question || !selectedAnswer) return;
    setBusy(true);
    try {
      const next = await learningApi.answerInterview({ session_id: interview.id, question_id: question.id, option_id: selectedAnswer, reflection, expected_revision: interview.revision });
      setProgress(next); setSelectedAnswer(''); setReflection('');
      setMessage(next.interview?.status === 'completed' ? 'Evaluation complete. Your course record has been updated.' : 'Response saved. Continue with the next interview question.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Your response could not be saved.', true); }
    finally { setBusy(false); }
  }

  const resetAllKnowledge = useCallback(async () => {
    setBusy(true);
    try {
      const freshProgress = await learningApi.resetKnowledge();
      try {
        const keysToRemove: string[] = [];
        for (let i = 0; i < localStorage.length; i++) {
          const key = localStorage.key(i);
          if (key && (key.startsWith('takumi-course-attempt:') || key === STORAGE_KEY)) {
            keysToRemove.push(key);
          }
        }
        keysToRemove.forEach(k => localStorage.removeItem(k));
      } catch { /* Storage might be disabled */ }

      setProgress(freshProgress);

      setSelectedAnswer('');
      setReflection('');
      setFailedAttempt(null);
      setActionCount(0);
      setResetEpoch(epoch => epoch + 1);
      setTab('dashboard');
      setShowResetConfirm(false);
      setMessage('Your watch progress, simulation result and interview have been reset.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unable to reset student knowledge.', true);
    } finally {
      setBusy(false);
    }
  }, [setMessage]);

  useEffect(() => {
    if (!showResetConfirm) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShowResetConfirm(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [showResetConfirm]);

  return <div className="learning-shell">
    {notification && <div className={`learning-notification${notification.isError ? ' is-error' : ''}`} role={notification.isError ? 'alert' : 'status'}>
      {notification.isError ? <AlertCircle size={16} /> : <Check size={16} />}
      <span>{notification.text}</span>
      <button type="button" aria-label="Dismiss notification" onClick={() => setNotification(null)}>×</button>
    </div>}
    <header className="learning-header">
      <a className="learning-brand" href="#" onClick={event => { event.preventDefault(); setTab('dashboard'); }} aria-label="Takumi learning dashboard">
        <img src={takumiLogo} alt="Takumi" className="learning-brand-logo" />
      </a>
      <nav className="learning-tabs" aria-label="Course modules">{tabs.map(({ id, label, icon: Icon }) => <button key={id} type="button" className={tab === id ? 'is-active' : ''} aria-current={tab === id ? 'page' : undefined} disabled={locked(id) || loading || !progress} title={locked(id) ? id === 'playground' ? 'Complete the course content to unlock' : 'Complete the playground to unlock' : label} onClick={() => setTab(id)}><Icon size={15} /><span>{label}</span>{locked(id) && <LockKeyhole size={12} />}</button>)}</nav>
      <div className="learning-header-user">
        <button
          type="button"
          className="learning-reset-btn"
          aria-label="Reset all student knowledge"
          onClick={() => setShowResetConfirm(true)}
          disabled={busy || loading}
          title="Reset all student knowledge and course progress"
        >
          <RotateCcw size={13} />
          <span>Reset knowledge</span>
        </button>
        <button className="learning-refresh-btn" type="button" aria-label="Check for course updates" title="Check for course updates" disabled={loading || busy} onClick={() => void checkCourseUpdates()}><RefreshCw size={14} /></button>
        <a className="learning-studio-link" href="/studio">Expert Studio</a>
        <span className="learning-profile"><span>AP</span>Apprentice</span>
      </div>
    </header>
    <main className={`learning-main ${tab === 'playground' ? 'learning-main-playground' : ''}`}>
      {loading || !course || !progress ? <section className="learning-empty"><BookOpen size={30} /><h1>{loading ? 'Opening your course' : 'Learning service unavailable'}</h1><p>{loading ? 'Loading your current course and saved progress.' : 'Connect the learning backend to load your course and continue from your saved stage.'}</p>{!loading && <button className="learning-button" onClick={() => void initialize()}><RefreshCw size={15} />Reconnect</button>}</section> : <>
        {tab !== 'playground' && <div className="learning-page-heading"><div><div className="learning-eyebrow">APPRENTICE LEARNING / BEARING ASSEMBLY</div><h1>{tab === 'dashboard' ? 'Your learning workspace' : tab === 'course' ? 'Course content' : 'Virtual interview'}</h1></div><div className="learning-heading-actions">{tab === 'dashboard' && <button type="button" className="learning-button-secondary learning-reset-page-btn" aria-label="Reset student knowledge" onClick={() => setShowResetConfirm(true)} disabled={busy || loading}><RotateCcw size={13} /><span>Reset student knowledge</span></button>}<span className="learning-record"><span className="learning-live-dot" />Progress saved to your course record</span></div></div>}
        {tab === 'dashboard' && <div className="learning-dashboard">
          <section className="learning-panel learning-current-course">
            <div className="learning-panel-heading"><span className="learning-eyebrow">CURRENT COURSE</span><span className="learning-tag">Foundation</span></div>
            <div className="learning-course-hero"><div><h2>{course.title}</h2><p>{course.description}</p><div className="learning-course-meta"><span><Box size={14} /> {course.equipment}</span><span><BookOpen size={14} /> 3 sequential stages</span></div><button className="learning-button" onClick={goNext}>{completed === 3 ? 'View evaluation' : completed ? 'Continue learning' : 'Open course'}<ArrowRight size={15} /></button></div><div className="learning-bearing-illustration" aria-hidden="true"><div className="learning-bearing-ring">{Array.from({ length: 8 }, (_, i) => <i key={i} style={{ transform: `rotate(${i * 45}deg) translateY(-51px)` }} />)}<span /></div><small>BALL BEARING · ASSEMBLY</small></div></div>
            <div className="learning-progress-summary"><div><strong>{completed} of 3 stages complete</strong><span>{Math.round(completed / 3 * 100)}%</span></div><progress max={3} value={completed} aria-label="Course stages completed" /></div>
          </section>
          <section className="learning-panel learning-path"><div className="learning-panel-heading"><h2>Learning sequence</h2><span className="learning-subtle">In order</span></div>{tabs.slice(1).map(({ id, label, icon: Icon }, index) => <button className="learning-stage" key={id} disabled={locked(id)} onClick={() => setTab(id)}><span className={`learning-stage-number ${completed > index ? 'is-complete' : ''}`}>{completed > index ? <Check size={15} /> : `0${index + 1}`}</span><span><strong>{label}</strong><small>{stageStatus(index)}</small></span>{locked(id) ? <LockKeyhole size={15} /> : <ChevronRight size={16} />}</button>)}</section>
          <section className="learning-panel learning-objectives"><div className="learning-panel-heading"><h2>What you will practice</h2><ClipboardCheck size={17} /></div><div className="learning-objective-grid"><div><span>01</span><h3>Inspect & prepare</h3><p>Identify the components and record observations before assembly.</p></div><div><span>02</span><h3>Assemble & verify</h3><p>Follow the guided sequence and check the supplied evidence.</p></div><div><span>03</span><h3>Explain your judgment</h3><p>Support a release or escalation decision in the final interview.</p></div></div></section>
          <section className="learning-panel learning-record-card"><div className="learning-panel-heading"><h2>Latest assessment</h2><ClipboardCheck size={17} /></div>{progress.playground_result ? <><div className="learning-score-grid">{Object.entries(progress.playground_result.scores).map(([label, value]) => <div key={label}><strong>{value}<small>/100</small></strong><span>{label}</span></div>)}</div><p>{interview?.status === 'completed' ? 'Virtual interview completed. Open the interview tab to review your evaluation.' : 'Playground result recorded. Continue to the interview to explain your decisions.'}</p><button className="learning-text-button" onClick={() => setTab('interview')}>Open interview <ArrowRight size={14} /></button></> : <div className="learning-no-result"><ClipboardCheck size={23} />{progress.manual_completion?.playground ? <div><strong className="learning-manual-label">Manually completed</strong><p>No simulation score was recorded.</p><button className="learning-text-button" onClick={() => setTab('interview')}>{interview?.status === 'completed' ? 'View evaluation' : 'Continue to interview'}<ArrowRight size={14} /></button></div> : <p>Your playground scores and final evaluation will appear here.</p>}</div>}</section>
        </div>}
        {tab === 'course' && <div className="learning-course-layout"><section className="learning-player-panel"><div className="learning-section-toolbar"><span>{progress.manual_completion?.course ? 'Manually completed' : progress.course_completed ? 'Course viewing complete' : 'Course section'}</span><div>{!progress.course_completed && <button className="learning-button learning-button-secondary" disabled={busy || !course.release_id} title={!course.release_id ? 'Publish a reviewed course release in Expert Studio before completing training' : undefined} onClick={() => void completeSection('course')}>Mark section complete<Check size={15} /></button>}{progress.playground_unlocked && <button className="learning-button" onClick={() => setTab('playground')}>Continue to playground<ArrowRight size={15} /></button>}</div></div><CoursePlayer key={`${course.release_id}:${course.release_content_hash}:${courseRecordingIdentity(course.video_url)}:${course.video_duration_seconds}`} course={course} progress={progress} onProgress={updateProgress} onMessage={setMessage} /></section></div>}
        {tab === 'playground' && <div className="learning-embedded-workspace"><Playground key={`playground-${resetEpoch}`} persistenceKey={`takumi-course-attempt:${course.release_id}:${course.release_content_hash ?? ''}:${courseRecordingKey(course.video_url ?? '')}:${course.video_duration_seconds}`} embedded onActivity={setActionCount} onMessage={setMessage} onAttemptComplete={recordAttempt} onContinueToInterview={() => { if (progress.interview_unlocked) setTab('interview'); else setMessage('Complete the playground section before continuing to the interview.'); }} headerAction={<>{!playgroundCompleted && <button type="button" className="learning-button learning-button-secondary header-complete-btn" disabled={busy || !course.release_id} title={!course.release_id ? 'Publish a reviewed course release in Expert Studio before completing training' : undefined} onClick={() => void completeSection('playground')}>Mark section complete<Check size={14} /></button>}{progress.interview_unlocked && <button type="button" className="learning-button header-interview-btn" onClick={() => setTab('interview')}>Continue to interview<ArrowRight size={14} /></button>}</>} /></div>}
        {tab === 'interview' && (
          <div className="learning-interview-layout">
            <section className="learning-panel learning-interview-panel">
              {interviewMode === 'live' ? (
                <>
                  <div className="learning-panel-heading">
                    <div>
                      <h2>Live AI Technical Viva</h2>
                    </div>
                    <span className="learning-tag">Voice examination</span>
                    <button
                      type="button"
                      className="learning-button-secondary"
                      style={{ marginLeft: 'auto', fontSize: 11, padding: '3px 10px' }}
                      onClick={() => setInterviewMode('choice')}
                    >
                      ← Change format
                    </button>
                  </div>
                  <VirtualInterview
                    token={liveToken}
                    onEnd={() => {
                      void learningApi
                        .getProgress()
                        .then((next) => {
                          setProgress(next);
                          setInterviewMode('choice');
                          setMessage(
                            'Interview completed. Your evaluation report is saved below and on the dashboard.'
                          );
                        })
                        .catch((error) =>
                          setMessage(
                            error instanceof Error
                              ? error.message
                              : 'Could not load saved evaluation.',
                            true
                          )
                        );
                    }}
                  />
                </>
              ) : (
                <>
                  <div className="learning-panel-heading">
                    <h2>
                      {interview?.status === 'completed'
                        ? 'Course evaluation'
                        : interviewMode === 'choice'
                          ? 'Virtual interview'
                          : 'Technical viva'}
                    </h2>
                    <span className="learning-tag">
                      {interview?.status === 'completed'
                        ? 'Completed'
                        : interviewMode === 'choice'
                          ? 'Choose format'
                          : 'Structured interview'}
                    </span>
                    {interviewMode === 'structured' && (
                      <button
                        type="button"
                        className="learning-button-secondary"
                        style={{ marginLeft: 'auto', fontSize: 11, padding: '3px 10px' }}
                        onClick={() => setInterviewMode('choice')}
                      >
                        ← Formats
                      </button>
                    )}
                  </div>

                  {interviewMode === 'choice' && (
                    <div className="learning-interview-intro">
                      <span className="learning-interview-icon">
                        <MessageSquare size={28} />
                      </span>
                      <h2>Explain the decisions you made</h2>
                      <p>
                        Select your preferred technical interview format to validate your bearing assembly judgment.
                      </p>

                      <div className="interview-choice-grid">
                        <div className="interview-choice-card">
                          <div className="interview-choice-card-header">
                            <div className="interview-choice-icon">
                              <MessageSquare size={18} />
                            </div>
                            <span className="learning-tag">Self-paced</span>
                          </div>
                          <h3>Structured Written Viva</h3>
                          <p>
                            Sequential questions evaluating your inspection checks, clearance tolerances, and simulation limits with written reflections.
                          </p>
                          {interview?.status === 'completed' ? (
                            <button
                              className="learning-button learning-button-secondary"
                              onClick={() => setInterviewMode('structured')}
                            >
                              <ClipboardCheck size={14} />
                              View evaluation
                            </button>
                          ) : interview ? (
                            <button
                              className="learning-button"
                              onClick={() => setInterviewMode('structured')}
                            >
                              Continue assessment
                              <ArrowRight size={14} />
                            </button>
                          ) : (
                            <button
                              disabled={busy}
                              className="learning-button"
                              onClick={() => {
                                setInterviewMode('structured');
                                void startInterview();
                              }}
                            >
                              Begin assessment
                              <ArrowRight size={14} />
                            </button>
                          )}
                        </div>

                        <div className="interview-choice-card">
                          <div className="interview-choice-card-header">
                            <div className="interview-choice-icon">
                              <Mic size={18} />
                            </div>
                            <span className="learning-tag">2-Min Live Viva</span>
                          </div>
                          <h3>Live AI Voice Interview</h3>
                          <p>
                            Interactive oral examination with Gemini AI examiner speaking via TTS. Evaluates answers against approved release knowledge and video citations.
                          </p>
                          <button
                            disabled={busy}
                            className="learning-button"
                            onClick={async () => {
                              try {
                                const t = await learningApi.getToken();
                                setLiveToken(t);
                                setInterviewMode('live');
                              } catch {
                                setMessage(
                                  'Unable to start live interview. Check that the backend is running.',
                                  true
                                );
                              }
                            }}
                          >
                            <Mic size={14} />
                            Start voice viva
                            <ArrowRight size={14} />
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {interviewMode === 'structured' && !interview && (
                    <div className="learning-interview-intro">
                      <span className="learning-interview-icon">
                        <MessageSquare size={28} />
                      </span>
                      <h2>Explain the decisions you made</h2>
                      <p>
                        Answer short, evidence-based questions about your assembly checks, release decision, and the limits of the simulation.
                      </p>
                      <button
                        disabled={busy}
                        className="learning-button"
                        onClick={() => void startInterview()}
                      >
                        Start virtual interview
                        <ArrowRight size={15} />
                      </button>
                    </div>
                  )}

                  {interviewMode === 'structured' && question && (
                    <div className="learning-question">
                      <div className="learning-eyebrow">
                        QUESTION {interview!.answers.length + 1}
                      </div>
                      <h2>{question.prompt}</h2>
                      {question.source_context && (
                        <details className="learning-script-source">
                          <summary>Review approved course evidence</summary>
                          <p>{question.source_context}</p>
                        </details>
                      )}
                      <fieldset disabled={busy}>
                        <legend className="learning-sr-only">Select your response</legend>
                        {question.options.map((option) => (
                          <label
                            className={selectedAnswer === option.id ? 'is-selected' : ''}
                            key={option.id}
                          >
                            <input
                              type="radio"
                              name={question.id}
                              value={option.id}
                              checked={selectedAnswer === option.id}
                              onChange={() => setSelectedAnswer(option.id)}
                            />
                            <span>{option.label}</span>
                          </label>
                        ))}
                      </fieldset>
                      <label className="learning-reflection">
                        Explain your reasoning <span>(optional)</span>
                        <textarea
                          maxLength={2000}
                          rows={3}
                          placeholder="Refer to the observations and checks you completed…"
                          value={reflection}
                          onChange={(event) => setReflection(event.target.value)}
                        />
                      </label>
                      <button
                        className="learning-button"
                        disabled={busy || !selectedAnswer}
                        onClick={() => void submitAnswer()}
                      >
                        {busy ? 'Saving response…' : 'Submit response'}
                        <ArrowRight size={15} />
                      </button>
                    </div>
                  )}

                  {interviewMode === 'structured' && interview?.result && (
                    <div className="learning-evaluation">
                      <div className="learning-evaluation-score">
                        <Check size={23} />
                        <div>
                          <strong>
                            {interview.result.explanation_points} /{' '}
                            {interview.result.available_points}
                          </strong>
                          <span>Interview assessment points</span>
                        </div>
                      </div>
                      <div className="learning-evaluation-row">
                        <span>Simulation limits recognized</span>
                        <strong>{interview.result.recognition_of_limits} points</strong>
                      </div>
                      <div className="learning-evaluation-row">
                        <span>Playground release decision</span>
                        <strong>
                          {interview.result.playground_assessed === false ||
                            !progress.playground_result
                            ? 'No simulation result'
                            : interview.result.critical_playground_error
                              ? 'Review needed'
                              : 'Recorded'}
                        </strong>
                      </div>
                      <button
                        className="learning-button learning-button-secondary"
                        onClick={downloadReport}
                      >
                        Download evaluation report
                      </button>
                      <h3>Review your responses</h3>
                      <div className="learning-answer-review">
                        {interview.answers.map((answer, index) => (
                          <article key={answer.question_id}>
                            <strong>
                              Question {index + 1} <span>{answer.points} points</span>
                            </strong>
                            {answer.question_prompt && (
                              <p>
                                <strong>{answer.question_prompt}</strong>
                              </p>
                            )}
                            {answer.selected_answer && (
                              <p>Selected: {answer.selected_answer}</p>
                            )}
                            <p>{answer.feedback}</p>
                            <small>
                              Evidence: {answer.evidence_ids.join(', ') || 'No linked source'}
                            </small>
                            {answer.reflection && <blockquote>{answer.reflection}</blockquote>}
                          </article>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
            </section>

            <aside className="learning-panel learning-interview-context">
              <div className="learning-panel-heading">
                <h2>Playground evidence</h2>
                <ClipboardCheck size={17} />
              </div>
              {progress.playground_result && (
                <>
                  <span className="learning-eyebrow">
                    {progress.playground_result.scenario === 'standard'
                      ? 'A CLEAN ASSEMBLY'
                      : 'THE UNCERTAIN FIT'}
                  </span>
                  <h3>Your recorded result</h3>
                  <p>{progress.playground_result.outcome}</p>
                  <div className="learning-score-grid">
                    {Object.entries(progress.playground_result.scores).map(([label, value]) => (
                      <div key={label}>
                        <strong>
                          {value}
                          <small>/100</small>
                        </strong>
                        <span>{label}</span>
                      </div>
                    ))}
                  </div>
                </>
              )}
              {!progress.playground_result && progress.manual_completion?.playground && (
                <div className="learning-manual-evidence">
                  <span className="learning-manual-label">Manually completed</span>
                  <p>
                    No simulation attempt was assessed. This interview will evaluate your course understanding.
                  </p>
                </div>
              )}
              {course.evidence_items && (
                <details className="learning-approved-evidence">
                  <summary>Approved source evidence</summary>
                  {course.evidence_items.map((item) => (
                    <article key={item.id}>
                      <strong>
                        {item.id} · {item.author}
                      </strong>
                      <p>{item.content || 'Recording segment'}</p>
                      <small>{item.source_label}</small>
                    </article>
                  ))}
                </details>
              )}
              <div className="learning-interview-guidance">
                <h3>When answering</h3>
                <p>
                  Describe what you observed, the checks you completed and how the evidence supports your decision.
                </p>
              </div>
              {interview && (
                <div className="learning-outline-item">
                  <Check size={16} />
                  <span>{interview.answers.length} responses saved</span>
                </div>
              )}
            </aside>
          </div>
        )}
        {tab === 'dashboard' && <FinalEvaluation progress={progress} onDownload={downloadReport} />}
      </>}
    </main>
    <footer className="learning-status" aria-label="Learning status">
      <div className="learning-status-content">
        <div>
          <ShieldCheck size={15} />
          <span className="learning-status-message" title={message}>{message}</span>
          {tab === 'playground' && <span className="learning-action-count">{actionCount} recorded actions</span>}
          {failedAttempt && <button disabled={busy} onClick={() => void recordAttempt(failedAttempt)}>{busy ? 'Saving…' : 'Retry saving result'}</button>}
        </div>
        <div>
          <span>{interview?.result?.notice || 'Illustrative training · Structured assessment · Instructor review required'}</span>
          {course && <span>{course.video_configured ? 'Course recording connected' : 'Awaiting course video URL'}</span>}
        </div>
      </div>
      <div className="learning-status-brand">
        <img src={jetroLogo} alt="JETRO" className="learning-status-jetro-logo" />
      </div>
    </footer>
    {showResetConfirm && (
      <div className="learning-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="reset-knowledge-title">
        <div className="learning-modal-dialog">
          <div className="learning-modal-header">
            <div className="learning-modal-icon">
              <RotateCcw size={20} />
            </div>
            <div>
              <h2 id="reset-knowledge-title">Reset all student knowledge?</h2>
              <p className="learning-modal-sub">This action cannot be undone.</p>
            </div>
          </div>
          <div className="learning-modal-body">
            <p>
              This will permanently clear all your course watch time, simulation playground scores and inspected components, and virtual interview evaluations. All 3 learning stages will return to their starting baseline.
            </p>
          </div>
          <div className="learning-modal-actions">
            <button
              type="button"
              className="learning-button-secondary"
              onClick={() => setShowResetConfirm(false)}
              disabled={busy}
            >
              Cancel
            </button>
            <button
              type="button"
              className="learning-button-danger"
              onClick={() => void resetAllKnowledge()}
              disabled={busy}
            >
              {busy ? 'Resetting…' : 'Confirm reset'}
            </button>
          </div>
        </div>
      </div>
    )}
  </div>;
}

