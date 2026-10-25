import type { LearningProgress } from '../learning-api';

export default function FinalEvaluation({ progress, onDownload }: { progress: LearningProgress; onDownload: () => void }) {
  const evaluation = progress.evaluation;
  const interview = progress.interview;
  const result = interview?.result;
  const score = (value: number | null | undefined) => value == null ? 'Not assessed' : `${value} / 100`;
  return <section className="learning-panel learning-final-evaluation" aria-label="Final evaluation report">
    <div className="learning-panel-heading"><h2>{result ? 'Final evaluation report' : 'Assessment progress'}</h2>
      {result && <button className="learning-button learning-button-secondary" onClick={onDownload}>Download report</button>}
    </div>
    <div className="learning-final-scores">
      <div><span>Playground</span><strong>{score(evaluation?.playground_score)}</strong></div>
      <div><span>Interview</span><strong>{score(evaluation?.interview_score)}</strong></div>
      <div><span>Combined evaluation</span><strong>{score(evaluation?.overall_score)}</strong></div>
    </div>
    <p>{evaluation?.weighting ?? '50% playground average + 50% interview'}. Playground score averages procedure, judgment and knowledge.</p>
    {progress.playground_result && <p>Playground outcome: {progress.playground_result.outcome}</p>}
    {evaluation?.critical_playground_error && <p className="learning-evaluation-review">A critical playground error requires instructor review, regardless of the combined score.</p>}
    {!progress.playground_result && <p>No assessed playground attempt is recorded. A manually completed section does not receive a score.</p>}
    {result?.summary && <p>{result.summary}</p>}
    {result && !result.available_points && <p>The interview has no assessable score. The combined evaluation remains unavailable.</p>}
    {!result && <p>Complete an interview to receive the final report.</p>}
    {result && <><p>{result.notice}</p>
      <details className="learning-answer-review"><summary>Interview answers and feedback ({interview!.answers.length})</summary>
        {interview!.answers.map((answer, index) => <article key={answer.question_id}>
          <strong>Question {index + 1} · {answer.points} points</strong>
          <p>{answer.question_prompt}</p><p>Response: {answer.selected_answer ?? answer.option_id}</p>
          <p>{answer.feedback}</p><small>Evidence: {answer.evidence_ids.join(', ') || 'No linked source'}</small>
          {answer.reflection && <blockquote>{answer.reflection}</blockquote>}
        </article>)}
      </details>
    </>}
  </section>;
}
