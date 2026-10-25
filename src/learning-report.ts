import type { LearningCourse, LearningProgress } from './learning-api';

export function createLearningReport(course: LearningCourse, progress: LearningProgress) {
  const interview = progress.interview;
  const rows = [
    `# ${course.title} — learning report`, '',
    `Release: ${course.release_id ?? 'Unavailable'}${course.release_version ? ` (v${course.release_version})` : ''}`,
    `Source: ${course.source_label}`,
    `Content fingerprint: ${course.release_content_hash ?? 'Unavailable'}`,
    `Course: ${progress.manual_completion?.course ? 'Manually completed' : progress.course_completed ? 'Viewing completed' : 'Incomplete'}`,
    `Playground: ${progress.playground_result ? 'Assessed simulation attempt' : progress.manual_completion?.playground ? 'Manually completed — no simulation score' : 'Incomplete'}`, '',
  ];
  if (progress.playground_result) {
    rows.push('## Simulation', '', `Outcome: ${progress.playground_result.outcome}`,
      ...Object.entries(progress.playground_result.scores).map(([label, score]) => `${label}: ${score}/100`), '');
  }
  if (interview) {
    rows.push('## Interview', '', `Status: ${interview.status}`, `Source release: ${interview.source_release_id ?? course.release_id}`,
      `Script fingerprint: ${interview.script_hash ?? 'Unavailable'}`, `Preparation: ${interview.generation_method ?? 'Structured templates'}`, '');
    if (interview.result) rows.push(`Assessment: ${interview.result.explanation_points}/${interview.result.available_points}`, interview.result.notice, '');
    if (interview.result?.summary) rows.push(interview.result.summary, '');
    interview.answers.forEach((answer, index) => rows.push(`### Question ${index + 1}`, '',
      answer.question_prompt ?? answer.question_id, `Selected response: ${answer.selected_answer ?? answer.option_id}`,
      `Points: ${answer.points}`, `Feedback: ${answer.feedback}`,
      ...(answer.reflection ? [`Learner reflection (ungraded): ${answer.reflection}`] : []),
      `Evidence: ${answer.evidence_ids.join(', ') || 'No linked source'}`, ''));
    if (!interview.answers.length) interview.transcript?.forEach((turn, index) => rows.push(
      `### Unscored response ${index + 1}`, '', turn.question, turn.answer, '',
    ));
  }
  if (progress.evaluation) {
    const score = (value: number | null) => value === null ? 'Not assessed' : `${value}/100`;
    const evaluation = progress.evaluation;
    rows.push('## Combined evaluation', '',
      `Playground: ${score(evaluation.playground_score)}`,
      `Interview: ${score(evaluation.interview_score)}`,
      `Overall: ${score(evaluation.overall_score)}`, `Weighting: ${evaluation.weighting}`,
      ...(evaluation.critical_playground_error ? ['Critical playground error: instructor review required.'] : []), '');
  }
  rows.push('## Approved source evidence', '');
  course.evidence_items?.forEach(item => rows.push(`- ${item.id} — ${item.author} (${item.source_label}): ${item.content ?? 'Recording segment'}`));
  return rows.join('\n');
}
