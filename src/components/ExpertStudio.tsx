import { useEffect, useState } from 'react';
import { studioApi, type AnalysisConfig, type AnalysisRun, type StudioOverview, type StudioUnit, type TacitField } from '../studio-api';
import '../studio.css';
import takumiLogo from '../assets/Takumi_Horizontal_Transparent_White.png';
import jetroLogo from '../assets/jetro-logo-png_seeklogo-75464.png';
import StudioAnalysisHistory from './StudioAnalysisHistory';
import StudioLearningGaps from './StudioLearningGaps';
import StudioFieldProvenance from './StudioFieldProvenance';
import StudioResetDialog from './StudioResetDialog';
import StudioCourseSources from './StudioCourseSources';
import StudioProposalSources from './StudioProposalSources';

const fields: { key: TacitField; label: string }[] = [{ key: 'action', label: 'Action' }, { key: 'cue', label: 'Observed cue' }, { key: 'interpretation', label: 'Interpretation' }, { key: 'decision', label: 'Decision' }, { key: 'exception', label: 'Exception / limits' }];
const errorText = (e: unknown) => e instanceof Error ? e.message : 'The action could not be completed.';

function extractedUnits(analysis: AnalysisRun): StudioUnit[] {
  const units = new Map<string, StudioUnit>();
  for (const proposal of analysis.proposals) {
    const unit = units.get(proposal.knowledge_unit_id) ?? { id: proposal.knowledge_unit_id, action: '', cue: null, interpretation: null, decision: null, exception: null, evidence_ids: [], review_status: 'AI proposal' };
    unit[proposal.field] = proposal.after; units.set(unit.id, unit);
  }
  return [...units.values()];
}


export default function ExpertStudio({ administration = false }: { administration?: boolean }) {
  const role = administration ? 'admin' : 'expert';
  const [data, setData] = useState<StudioOverview | null>(null);
  const [config, setConfig] = useState<AnalysisConfig | null>(null);
  const [run, setRun] = useState<AnalysisRun | null>(null);
  const [sourceNotes, setSourceNotes] = useState('');
  const [requestMode, setRequestMode] = useState<'live' | 'rehearsal'>('live');
  const [useCourseContent, setUseCourseContent] = useState(true);
  const [includeReviewNotes, setIncludeReviewNotes] = useState(true);
  const [courseDirty, setCourseDirty] = useState(false);
  const [identity, setIdentity] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [edit, setEdit] = useState<StudioUnit | null>(null);
  const [note, setNote] = useState('');
  const [answer, setAnswer] = useState('');
  const [deferUnanswered, setDeferUnanswered] = useState(false);
  const [answerField, setAnswerField] = useState<TacitField | ''>('');
  const [busy, setBusy] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [message, setMessage] = useState('Run a diagnosis, review the proposed changes, then approve publication to the apprentice course.');
  const [error, setError] = useState(false);
  const [view, setView] = useState<'diagnosis' | 'knowledge' | 'audit'>('diagnosis');
  const original = data?.draft.knowledge_units.find(unit => unit.id === edit?.id);
  const dirty = !!edit && !!original && fields.some(({ key }) => (edit[key] || null) !== (original[key] || null));
  const pending = data?.draft.clarifications.filter(item => item.status === 'pending') ?? [];
  const taskId = data?.task.id ?? '';
  const working = busy || run?.status === 'running';
  const unsaved = dirty || !!answer.trim() || courseDirty;
  const diagnosisBlockReason = busy ? 'Wait for the current save or loading operation to finish.'
    : run?.status === 'running' ? `Diagnosis is already running: ${run.stage.replaceAll('_', ' ')}.`
    : !config ? 'Wait for the Studio configuration to load.'
    : !config.enabled ? 'AI diagnosis is disabled in the backend configuration.'
    : requestMode === 'live' && config.mode !== 'live' ? 'Live AI needs GEMINI_API_KEY in takumi-backend/.env. Restart the backend after configuring it.'
    : courseDirty ? 'Save or discard your course script or expert review notes before running diagnosis.'
    : dirty ? 'Save or discard your draft field edits before running diagnosis.'
    : answer.trim() ? 'Record or discard your clarification response before running diagnosis.'
    : requestMode === 'live' && useCourseContent && !config.course_content ? 'Restart npm run dev and refresh: the running backend does not support course uploads yet.'
    : requestMode === 'live' && useCourseContent && !config.course_content?.available ? 'Upload a course video or save the course script above before running diagnosis.'
    : (requestMode !== 'live' || !useCourseContent) && !sourceNotes.trim() ? 'Enter source notes, or select uploaded course content, before running diagnosis.'
    : null;

  useEffect(() => {
    if (!unsaved) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [unsaved]);

  async function refresh() {
    const { overview, analysis: settings, latest_run: latest, user } = await studioApi.workspace(role);
    setData(overview); setConfig(settings); setRun(latest?.mode === requestMode ? latest : null);
    setIdentity(user.display_name);
    setEdit(previous => overview.draft.knowledge_units.find(unit => unit.id === previous?.id) ?? overview.draft.knowledge_units[0] ?? null);
    return settings;
  }
  useEffect(() => {
    let alive = true;
    setBusy(true);
    studioApi.workspace(role)
      .then(({ overview, analysis: settings, latest_run: latest, user }) => { if (alive) { setUseCourseContent(!!settings.course_content?.available); setData(overview); setEdit(overview.draft.knowledge_units[0] ?? null); setConfig(settings); setSourceNotes(latest?.mode === 'live' ? latest.source_notes : settings.source_notes); setIdentity(user.display_name); setRun(latest?.mode === 'live' ? latest : null); setSelected(latest?.mode === 'live' ? latest.proposals.map(item => item.id) : []); } })
      .catch(e => { if (alive) { setMessage(errorText(e)); setError(true); } }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [role]);
  useEffect(() => {
    if (run?.status !== 'running') return;
    let alive = true;
    const timer = window.setInterval(() => { void studioApi.latestAnalysis(role, taskId).then(latest => { if (alive && latest) { setRun(latest); if (latest.status === 'ready') setSelected(latest.proposals.map(item => item.id)); } }).catch(e => { if (alive) { setMessage(errorText(e)); setError(true); } }); }, 1200);
    return () => { alive = false; window.clearInterval(timer); };
  }, [run?.status, role, taskId]);
  useEffect(() => {
    if (!taskId || !run || run.status === 'running') return;
    let alive = true;
    studioApi.overview(role, taskId).then(overview => { if (alive) setData(overview); }).catch(e => { if (alive) { setMessage(errorText(e)); setError(true); } });
    return () => { alive = false; };
  }, [role, taskId, run?.id, run?.status]);
  async function act(action: () => Promise<unknown>, success: string) {
    setBusy(true); setError(false);
    try { await action(); await refresh(); setMessage(success); }
    catch (e) { setMessage(errorText(e)); setError(true); }
    finally { setBusy(false); }
  }
  function guard(action: () => void) {
    if (unsaved) { setMessage('Save or discard your current edits and clarification response before changing views.'); setError(true); return; }
    action();
  }
  function publication() {
    try { localStorage.setItem('takumi-publication', JSON.stringify({ task_id: taskId, at: Date.now() })); } catch { /* The apprentice also checks for updates periodically. */ }
    window.dispatchEvent(new Event('takumi-publication'));
  }
  async function resetWorkspace() {
    if (!data) return;
    setBusy(true); setError(false);
    try {
      await studioApi.reset(role, taskId, data.draft.revision);
      setCourseDirty(false); setUseCourseContent(true); setIncludeReviewNotes(true); setAnswer(''); setAnswerField(''); setNote(''); setSelected([]); setDeferUnanswered(false);
      setSourceNotes(''); setRun(null); setEdit(null); setRequestMode('live'); setView('diagnosis');
      await refresh(); publication();
      setMessage('Expert panel data reset. Enter source notes and run live AI to create a new draft.');
    } catch (e) { setMessage(errorText(e)); setError(true); }
    finally { setBusy(false); setResetOpen(false); }
  }
  async function diagnose() {
    if (!data || unsaved) return;
    setBusy(true); setError(false); setMessage('Diagnosis requested. Source analysis, extraction and patch evaluation run on the server.');
    const timer = window.setInterval(() => { void studioApi.latestAnalysis(role, taskId).then(latest => { if (latest) setRun(latest); }).catch(() => {}); }, 1200);
    try {
      const result = await studioApi.analyze(role, taskId, data.draft.revision, sourceNotes, requestMode, requestMode === 'live' && useCourseContent, includeReviewNotes);
      setRun(result); setSelected(result.proposals.map(item => item.id));
      setMessage(result.status === 'failed' ? result.error?.message ?? 'Diagnosis failed. No changes were published.' : 'Diagnosis complete. Review source quotes, patches and evaluation before approving publication.');
      setError(result.status === 'failed');
    } catch (e) { setMessage(errorText(e)); setError(true); await studioApi.latestAnalysis(role, taskId).then(setRun).catch(() => {}); }
    finally { window.clearInterval(timer); setBusy(false); }
  }
  function save() {
    if (!data || !edit || !original) return;
    const changes: Partial<Record<TacitField, string | null>> = {};
    fields.forEach(({ key }) => { const value = edit[key]?.trim() || null; if (value !== original[key]) changes[key] = value; });
    void act(() => studioApi.save(role, taskId, data.draft.revision, edit, changes), 'Draft saved. Evaluate the new revision before publishing.');
  }
  const evaluated = !!data?.evaluation?.passed && data.evaluation.revision === data.draft.revision && !data.evaluation.stale;
  const current = run?.revision === data?.draft.revision;
  const sourceMatches = run?.evaluation?.source_manifest ? useCourseContent && run.evaluation.source_manifest.input_hash === config?.course_source_hashes?.[includeReviewNotes ? 'with_review_notes' : 'without_review_notes'] : (!useCourseContent || requestMode === 'rehearsal') && sourceNotes === run?.source_notes;
  const canApply = run?.status === 'ready' && run.mode === requestMode && run.evaluation?.passed && current && sourceMatches && selected.length > 0;
  const unitProposals = run?.proposals.filter(proposal => proposal.knowledge_unit_id === edit?.id) ?? [];
  const selectedUnits = new Set(run?.proposals.filter(p => selected.includes(p.id)).map(p => p.knowledge_unit_id));
  const newQuestions = run?.evaluation?.learning_gaps?.filter(gap => gap.status === 'expert_required' && selectedUnits.has(gap.knowledge_unit_id)).length ?? 0;
  const unansweredCount = pending.length + newQuestions;
  const canPublish = run?.status === 'ready' && run.mode === requestMode && run.evaluation?.passed && current && sourceMatches && selected.length > 0 && !unsaved && (!unansweredCount || deferUnanswered);
  const previewingCourse = !!(run?.status === 'ready' && run.evaluation?.replace_draft && current && sourceMatches);
  const reviewUnits = previewingCourse && run ? extractedUnits(run) : data?.draft.knowledge_units ?? [];
  useEffect(() => {
    if (previewingCourse && run) { const units = extractedUnits(run); setEdit(previous => units.find(unit => unit.id === previous?.id) ?? units[0] ?? null); }
  }, [previewingCourse, run?.id]);
  const evidence = data?.evidence.filter(item => edit?.evidence_ids.includes(item.id)) ?? [];
  const publishedUnit = data?.active_release?.knowledge_units.find(unit => unit.id === edit?.id);

  return <div className="studio-shell">
    {resetOpen && data && <StudioResetDialog data={data} busy={busy} onCancel={() => setResetOpen(false)} onConfirm={() => void resetWorkspace()}/>}
    <header className="studio-header"><a className="studio-brand" href="/" aria-label="Takumi"><img src={takumiLogo} alt="Takumi" className="studio-brand-logo" /></a><nav aria-label="Studio modules">{(['diagnosis', 'knowledge', 'audit'] as const).map(item => <button key={item} className={view === item ? 'active' : ''} onClick={() => guard(() => setView(item))}>{item === 'diagnosis' ? 'AI diagnosis' : item === 'knowledge' ? 'Expert review' : 'Publication audit'}</button>)}<a href="/">Apprentice workspace ↗</a></nav><span className="studio-role">{identity || 'Connecting...'}</span></header>
    <main className="studio-main"><div className="studio-heading"><div><span className="studio-eyebrow">EXPERT KNOWLEDGE CAPTURE</span><h1>{administration ? 'Tutor administration' : 'Expert Studio'}</h1></div><div className="studio-heading-actions">{config?.reset_enabled && <button className="studio-danger" disabled={working} onClick={() => setResetOpen(true)}>Reset Expert data</button>}<span className="studio-connected">{data ? `${data.task.title} - connected course` : 'Loading connected course'}</span><button disabled={working || unsaved} onClick={() => void act(async () => { await refresh(); }, 'Latest draft and publication state loaded.')}>Refresh</button></div></div>
      {data ? <><section className="studio-summary" aria-label="Release workflow"><div><small>Current draft</small><strong>Revision {data.draft.revision}</strong><span>{data.task.source_label} source</span></div><div><small>Diagnosis mode</small><strong>{requestMode === 'rehearsal' ? 'Explicit rehearsal' : config?.mode === 'live' ? 'Live AI agents' : 'Configuration required'}</strong><span>{requestMode === 'rehearsal' ? 'Authored illustrative proposals' : config?.model}</span></div><div><small>Expert review</small><strong>{pending.length} pending clarification{pending.length === 1 ? '' : 's'}</strong><span>Review source and technical accuracy</span></div><div><small>Apprentice release</small><strong>{data.active_release ? `Version ${data.active_release.version}` : 'Unpublished'}</strong><span>Course and new interview scripts</span></div></section>
      {view === 'diagnosis' ? <div className="studio-diagnosis-layout">
        <section className="studio-panel studio-source"><div className="studio-panel-title"><h2>Expert source notes</h2><span>{requestMode === 'rehearsal' ? 'illustrative fixture' : config?.source_label}</span></div><div className="studio-scroll"><label className="studio-analysis-mode">Analysis mode<select aria-label="Analysis mode" value={requestMode} disabled={working} onChange={event => { const mode = event.target.value as 'live' | 'rehearsal'; guard(() => { setRequestMode(mode); if (mode === 'live') setUseCourseContent(!!config?.course_content?.available); setSourceNotes(mode === 'rehearsal' ? config?.rehearsal_source_notes ?? '' : config?.source_notes ?? ''); setRun(null); setSelected([]); setDeferUnanswered(false); }); }}><option value="live">Live AI agents</option>{config?.rehearsal_available && data.draft.knowledge_units.length > 0 && <option value="rehearsal">Illustrative rehearsal (no AI call)</option>}</select></label><p>{requestMode === 'rehearsal' ? 'This explicitly selected rehearsal uses authored fixture data.' : 'Uploaded course video and script are selected automatically. Add expert notes in Review and choose whether to include them.'}</p>{requestMode === 'live' && config && <>{!config.course_content && <p className="studio-backend-notice" role="status">Backend update required: restart npm run dev and refresh to load course video, script, and review-note support.</p>}<StudioCourseSources config={config} taskId={taskId} role={role} working={working} includeNotes={includeReviewNotes} setIncludeNotes={setIncludeReviewNotes} onBusy={setBusy} onDirty={setCourseDirty} onSaved={async () => { await refresh(); setUseCourseContent(true); setMessage('Course sources saved. Run extraction to review the updated tacit knowledge.'); }} onError={message => { setMessage(message); setError(true); }}/><label className="studio-defer"><input type="checkbox" checked={useCourseContent} disabled={working} onChange={event => { setUseCourseContent(event.target.checked); setRun(null); setSelected([]); }}/>Use uploaded course video and script automatically</label></>}{(!useCourseContent || requestMode === 'rehearsal') && <label className="studio-source-label">Source material<textarea aria-label="Expert source notes" value={sourceNotes} disabled={working} onChange={event => setSourceNotes(event.target.value)}/></label>}<p className="studio-mode-notice">{requestMode === 'rehearsal' ? 'Illustrative rehearsal: authored proposals; no AI service will be called.' : config?.notice}</p><ol className="studio-agent-steps">{config?.agent_roles.map(agent => <li key={agent.stage} aria-current={run?.stage === agent.stage ? 'step' : undefined}>{agent.label}{run?.stage === agent.stage ? ' - running' : ''}</li>)}</ol></div><div className="studio-panel-actions studio-diagnosis-actions"><span id="diagnosis-block-reason" className="studio-diagnosis-reason" role="status">{diagnosisBlockReason ?? 'Ready to extract tacit knowledge from the selected sources.'}</span><button className="studio-primary" aria-describedby="diagnosis-block-reason" title={diagnosisBlockReason ?? 'Run diagnosis'} disabled={!!diagnosisBlockReason} onClick={() => void diagnose()}>{working ? 'Diagnosis in progress…' : requestMode === 'live' ? 'Run AI diagnosis' : 'Run illustrative rehearsal'}</button></div></section>
        <section className="studio-panel studio-proposals"><div className="studio-panel-title"><h2>Proposed tacit changes</h2><span>{run?.proposals.length ?? 0} patches</span></div><div className="studio-scroll">{run ? <><div className="studio-run-state" role="status"><strong>{run.mode === 'live' ? 'AI agent run' : 'Rehearsal run'} · {run.status}</strong><span>{run.stage.replaceAll('_', ' ')} · draft r{run.revision}</span></div>{run.error && <p>{run.error.message}</p>}{<StudioLearningGaps run={run}/>}<h3>Proposed course patches</h3>{run.proposals.map(proposal => <article className="studio-proposal" key={proposal.id}><label className="studio-proposal-selection"><input type="checkbox" checked={selected.includes(proposal.id)} disabled={working || run.status !== 'ready'} onChange={event => setSelected(ids => event.target.checked ? [...ids, proposal.id] : ids.filter(id => id !== proposal.id))}/><strong>{fields.find(field => field.key === proposal.field)?.label} · {proposal.knowledge_unit_id}</strong></label><div className="studio-diff"><div><small>Current draft</small><p>{proposal.before || 'No guidance recorded'}</p></div><div><small>Proposed change</small><p>{proposal.after}</p></div></div><blockquote>{proposal.video_evidence && <small>Video evidence: {(proposal.video_evidence.start_ms / 1000).toFixed(1)}?{(proposal.video_evidence.end_ms / 1000).toFixed(1)}s ? {proposal.video_evidence.modality}</small>}<strong>Source quote</strong><p>{proposal.source_quote}</p></blockquote><p className="studio-rationale">{proposal.rationale}</p></article>)}</> : <div className="studio-diagnosis-empty"><h3>Diagnose the current exercise</h3><p>One action runs source analysis, tacit extraction and patch evaluation. Reviewable changes appear here before publication.</p></div>}</div></section>
        <section className="studio-panel studio-publish"><div className="studio-panel-title"><h2>Evaluate & publish</h2></div><div className="studio-scroll">{run?.evaluation?.replace_draft && <p>These extracts create a new draft from the uploaded course content. Saving or publishing replaces the current draft units. The active release changes only when you publish.</p>}{run?.evaluation ? <div className="studio-check-result"><strong>{run.evaluation.passed ? 'Patch evaluation passed' : 'Patch evaluation needs attention'}</strong><p>{run.evaluation.summary}</p>{run.evaluation.issues.map((issue, index) => <p key={index}>{typeof issue === 'string' ? issue : JSON.stringify(issue)}</p>)}</div> : <p>Evaluation appears after the agents finish. Publication requires successful evaluation and your explicit approval.</p>}{run && !current && run.status !== 'published' && run.status !== 'applied' && <p>This diagnosis belongs to an older revision. Run a new diagnosis.</p>}{run && run.status === 'ready' && !sourceMatches && <p>Course sources or extraction options changed. Run diagnosis again before approving these changes.</p>}{unansweredCount > 0 && <label className="studio-defer"><input type="checkbox" checked={deferUnanswered} disabled={working} onChange={event => setDeferUnanswered(event.target.checked)}/>Keep {unansweredCount} unanswered expert prompts as unknown and defer them for this demo</label>}<label className="studio-note">Publication note<textarea aria-label="Publication note" value={note} disabled={working} onChange={event => setNote(event.target.value)} maxLength={1000}/></label><button disabled={working || unsaved || !canApply || !note.trim()} onClick={() => void act(() => studioApi.applyAnalysis(role, taskId, run!.id, data.draft.revision, selected, note.trim()), 'Reviewed extracted fields saved to draft. Evaluate the revision before publication.')}>Save selected extracts to draft</button><p>Selected patches update the course and source for new interview sessions. Publication starts a fresh learning pathway for this release.</p><button className="studio-primary" disabled={working || !canPublish || !note.trim()} onClick={() => void act(async () => { await studioApi.publishAnalysis(role, taskId, run!.id, data.draft.revision, selected, note.trim(), deferUnanswered); publication(); }, 'Changes published. The apprentice course and new interview scripts now use this release.')}>Approve & publish changes</button>{run?.status === 'published' && <a className="studio-apprentice-cta" href="/">View updated apprentice course ↗</a>}</div></section>
      </div> : view === 'knowledge' ? <div className="studio-workbench">
        <section className="studio-panel studio-units"><div className="studio-panel-title"><h2>{previewingCourse ? 'Extracted knowledge units' : 'Knowledge units'}</h2><span>{reviewUnits.length}</span></div><div className="studio-scroll">{!reviewUnits.length && <p>No knowledge units yet. Enter source notes in AI diagnosis, run live extraction, and save the reviewed extracts to draft.</p>}{reviewUnits.map((unit, index) => <button key={unit.id} className={`studio-unit ${edit?.id === unit.id ? 'selected' : ''}`} onClick={() => guard(() => { setEdit({ ...unit }); setAnswer(''); setAnswerField(''); })}><small>{String(index + 1).padStart(2, '0')} · {unit.review_status}</small><strong>{unit.action}</strong><span>{unit.evidence_ids.length} evidence references</span></button>)}</div></section>
        <section className="studio-panel studio-editor"><div className="studio-panel-title"><h2>Tacit knowledge review</h2><span>{edit?.id}</span></div><div className="studio-scroll">{config && <StudioCourseSources config={config} taskId={taskId} role={role} working={working || dirty || !!answer.trim()} review includeNotes={includeReviewNotes} setIncludeNotes={setIncludeReviewNotes} onBusy={setBusy} onDirty={setCourseDirty} onSaved={async () => { await refresh(); setUseCourseContent(true); setMessage('Expert notes saved. Include them in the next course extraction.'); }} onError={message => { setMessage(message); setError(true); }}/>}<button disabled={working || unsaved || !config?.course_content?.available} onClick={() => setView('diagnosis')}>Review course extraction options</button>{!edit && <p>The draft is empty. Review new AI extracts in Diagnosis and save them to populate these fields.</p>}{edit && <>
          <section className="studio-extracted-review" aria-label="Extracted proposals for selected unit">
            <h3>{run?.mode === 'rehearsal' ? 'Illustrative rehearsal proposals' : 'AI extracted proposals'}</h3>
            {run && <p>{run.model ?? config?.model} / {run.status} / revision {run.revision}</p>}
            {unitProposals.length ? unitProposals.map(proposal => <article key={proposal.id} className="studio-review-proposal">
              <label><input type="checkbox" checked={selected.includes(proposal.id)} disabled={working || !canApply && run?.status !== 'ready'} onChange={event => setSelected(ids => event.target.checked ? [...ids, proposal.id] : ids.filter(id => id !== proposal.id))}/>{fields.find(field => field.key === proposal.field)?.label}</label>
              <p>{proposal.after}</p><blockquote>{proposal.source_quote}</blockquote><p>{proposal.rationale}</p>
            </article>) : <p>{run?.status === 'failed' ? run.error?.message : run?.status === 'ready' ? 'No changes proposed for this unit.' : 'Run AI diagnosis on the source material to extract reviewable changes for this unit.'}</p>}
            {unitProposals.length > 0 && <><label className="studio-note">Review note<textarea aria-label="AI review note" value={note} onChange={event => setNote(event.target.value)} disabled={working}/></label><button disabled={working || unsaved || !canApply || !note.trim()} onClick={() => void act(() => studioApi.applyAnalysis(role, taskId, run!.id, data.draft.revision, selected, note.trim()), 'Reviewed extracted fields saved to draft. Evaluate the revision before publication.')}>Save {selected.length} selected extracted change{selected.length === 1 ? '' : 's'} to draft</button></>}
            {run?.status === 'applied' && <p>Reviewed extracts are saved in the draft fields below.</p>}
            {run?.status === 'published' && <p>Reviewed extracts have been published.</p>}
            {run?.status === 'ready' && !current && <p>The draft changed. Run a new diagnosis before saving these proposals.</p>}
          </section>
          <div className="studio-field-grid">{fields.map(({ key, label }) => <label className={key === 'action' ? 'studio-field-wide' : ''} key={key}><span>{label}</span><textarea aria-label={label} disabled={working} value={edit[key] ?? ''} readOnly={previewingCourse} onChange={event => setEdit({ ...edit, [key]: event.target.value })} rows={key === 'action' ? 2 : 3}/>{previewingCourse ? <small>AI proposal: save the reviewed extracts to create field evidence.</small> : <StudioFieldProvenance unit={original ?? edit} field={key} evidence={data.evidence}/>}{publishedUnit && (publishedUnit[key] || null) !== (edit[key] || null) && <div className="studio-previous"><strong>Currently published</strong><p>{publishedUnit[key] || 'No guidance recorded'}</p></div>}</label>)}</div>{pending.filter(item => item.knowledge_unit_id === edit.id).map(item => <div className="studio-clarification" key={item.id}><strong>Expert clarification</strong><p>{item.question}</p><textarea aria-label="Clarification answer" value={answer} disabled={working} onChange={event => setAnswer(event.target.value)}/><label>Also apply answer to<select aria-label="Clarification target field" value={answerField} disabled={working} onChange={event => setAnswerField(event.target.value as TacitField | '')}><option value="">Evidence only — no field update</option>{fields.map(field => <option value={field.key} key={field.key}>{field.label}</option>)}</select></label><button disabled={working || dirty || !answer.trim()} onClick={() => void act(async () => { await studioApi.clarify(role, item.id, data.draft.revision, answer.trim(), 'answered', answerField ? { unitId: edit.id, field: answerField } : undefined); setAnswer(''); setAnswerField(''); }, 'Expert response recorded. Any selected field was updated explicitly; evaluate before publishing.')}>Record expert response</button></div>)}</>}</div><div className="studio-panel-actions"><span>{dirty ? 'Unsaved changes' : 'Changes require evaluation'}</span><button disabled={working || !unsaved} onClick={() => { setEdit(original ? { ...original } : null); setAnswer(''); setMessage('Unsaved edits discarded.'); }}>Discard edits</button><button className="studio-primary" disabled={working || !dirty} onClick={save}>Save draft changes</button></div></section>
        <aside className="studio-right"><section className="studio-panel"><div className="studio-panel-title"><h2>Source evidence</h2><span>{evidence.length}</span></div><div className="studio-scroll">{previewingCourse && run && <StudioProposalSources run={run} proposals={unitProposals}/>} {evidence.map(item => <article className="studio-evidence-item" key={item.id}><div><strong>{item.type.replaceAll('_', ' ')}</strong><span>{item.source_label}</span></div><p>{item.content || `Recording evidence${item.start_ms !== null ? ` at ${(item.start_ms / 1000).toFixed(1)}s` : ''}`}</p><small>{item.author} · {item.id}</small></article>)}</div></section><section className="studio-panel studio-approval"><div className="studio-panel-title"><h2>Draft revision review</h2></div><div className="studio-scroll"><p>{data.evaluation_scope}</p><button disabled={working || unsaved || previewingCourse} onClick={() => void act(() => studioApi.evaluate(role, taskId, data.draft.revision), 'Revision evaluated. Review checks before publishing.')}>Evaluate current revision</button>{data.evaluation && <div className="studio-check-result"><strong>{evaluated ? 'Evaluation passed' : 'Evaluation needs attention'}</strong>{data.evaluation.issues.map((issue, index) => <p key={index}>{issue.knowledge_unit_id}: {issue.problem}</p>)}</div>}<label className="studio-note">Publication note<textarea aria-label="Manual publication note" value={note} onChange={event => setNote(event.target.value)} disabled={working}/></label><button className="studio-primary" disabled={working || unsaved || !evaluated || previewingCourse || !note.trim()} onClick={() => void act(async () => { await studioApi.publish(role, taskId, data.draft.revision, note.trim()); publication(); }, 'Release published. Apprentice course and new interview scripts now use the reviewed guidance.')}>Approve & publish revision</button></div></section></aside>
      </div> : <div className="studio-audit-layout"><section className="studio-panel"><div className="studio-panel-title"><h2>Workflow & publication history</h2><span>{data.audit.length} events</span></div><div className="studio-scroll"><StudioAnalysisHistory runs={data.analysis_runs ?? []}/><h3>Recorded workflow events</h3>{!data.audit.length && <p>No workflow events recorded for this task.</p>}<table><thead><tr><th>Event</th><th>Actor</th><th>Time</th><th>Details</th></tr></thead><tbody>{data.audit.map(event => <tr key={event.id}><td>{event.action.replaceAll('_', ' ')}</td><td>{event.actor_id}</td><td>{new Date(event.created_at).toLocaleString()}</td><td><pre>{JSON.stringify(event.details, null, 2)}</pre></td></tr>)}</tbody></table></div></section><section className="studio-panel"><div className="studio-panel-title"><h2>Active apprentice release</h2></div><div className="studio-scroll">{data.active_release ? <dl><dt>Release</dt><dd>{data.active_release.id}</dd><dt>Version</dt><dd>{data.active_release.version}</dd><dt>Approved by</dt><dd>{data.active_release.approved_by}</dd><dt>Published</dt><dd>{new Date(data.active_release.approved_at).toLocaleString()}</dd><dt>Publication note</dt><dd>{data.active_release.approval_note}</dd><dt>Content fingerprint</dt><dd className="studio-hash">{data.active_release.content_hash}</dd></dl> : <p>No release published.</p>}</div></section></div>}
      </> : <div className="studio-empty"><h2>{busy ? 'Loading Expert Studio…' : 'Studio could not load'}</h2>{!busy && <button onClick={() => window.location.reload()}>Reconnect</button>}</div>}
    </main><footer className={`studio-status ${error ? 'error' : ''}`} aria-label="Studio status"><span role={error ? 'alert' : 'status'}>{message}</span><div className="studio-status-right"><span>Human approval before publication</span><img src={jetroLogo} alt="JETRO" className="studio-jetro-logo" /></div></footer>
  </div>;
}
