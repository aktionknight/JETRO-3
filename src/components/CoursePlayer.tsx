import { useEffect, useRef, useState } from 'react';
import { BookOpen, PlayCircle } from 'lucide-react';
import { learningApi } from '../learning-api';
import type { LearningCourse, LearningProgress } from '../learning-api';

interface CoursePlayerProps {
  course: LearningCourse;
  progress: LearningProgress;
  onProgress: (progress: LearningProgress) => void;
  onMessage: (message: string) => void;
}
const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;

export function CoursePlayer({ course, progress, onProgress, onMessage }: CoursePlayerProps) {
  // Refreshing a signed URL must not reload a recording already being watched.
  // The parent remounts this player only when the release or recording changes.
  const [videoUrl] = useState(course.video_url);
  const video = useRef<HTMLVideoElement>(null);
  const session = useRef<string | null>(null);
  const starting = useRef(false);
  const mounted = useRef(true);
  const maxWatched = useRef(progress.watched_seconds);
  const lastPosition = useRef(progress.watched_seconds);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const callbacks = useRef({ onProgress, onMessage });
  const progressRef = useRef(progress);
  const [connecting, setConnecting] = useState(false);
  callbacks.current = { onProgress, onMessage };
  progressRef.current = progress;

  const persist = () => {
    const player = video.current;
    const sessionId = session.current;
    if (!player || !sessionId) return;
    // Replaying an earlier section must not move the server's watched frontier backwards.
    const position = maxWatched.current;
    queue.current = queue.current.then(async () => {
      if (session.current !== sessionId) return;
      try {
        const updated = await learningApi.heartbeat(sessionId, position);
        if (mounted.current) callbacks.current.onProgress(updated);
      } catch (error) {
        session.current = null;
        if (mounted.current) {
          video.current?.pause();
          maxWatched.current = progressRef.current.watched_seconds;
          if (video.current) video.current.currentTime = maxWatched.current;
          callbacks.current.onMessage(error instanceof Error ? error.message : 'Course progress could not be saved.');
        }
      }
    });
  };

  useEffect(() => {
    mounted.current = true;
    const timer = window.setInterval(() => {
      const player = video.current;
      if (player && !player.paused && !player.seeking && document.visibilityState === 'visible') persist();
    }, 5000);
    const hide = () => { if (document.visibilityState !== 'visible') video.current?.pause(); };
    document.addEventListener('visibilitychange', hide);
    return () => {
      persist();
      mounted.current = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', hide);
    };
  }, []);

  const start = async () => {
    const player = video.current;
    if (!player || session.current) return;
    player.pause();
    if (starting.current) return;
    starting.current = true;
    setConnecting(true);
    try {
      const playback = await learningApi.startPlayback();
      if (!mounted.current) return;
      session.current = playback.session_id;
      maxWatched.current = playback.position_seconds;
      lastPosition.current = playback.position_seconds;
      if (Math.abs(player.currentTime - playback.position_seconds) > 0.1) player.currentTime = playback.position_seconds;
      callbacks.current.onProgress(playback.progress);
      if (document.visibilityState === 'visible') await player.play();
    } catch (error) {
      if (mounted.current) callbacks.current.onMessage(error instanceof Error ? error.message : 'The course recording could not be opened.');
    } finally {
      starting.current = false;
      if (mounted.current) setConnecting(false);
    }
  };

  const track = () => {
    const player = video.current;
    if (!player || player.seeking) return;
    const position = player.currentTime;
    if (!player.paused && document.visibilityState === 'visible' && position <= lastPosition.current + 2) {
      maxWatched.current = Math.max(maxWatched.current, position);
    }
    lastPosition.current = position;
  };
  const seek = () => {
    const player = video.current;
    if (!player || progressRef.current.course_completed) return;
    if (player.currentTime > maxWatched.current + 0.5) {
      // Restart an unwatched stream instead of seeking back into an unavailable
      // zero-time keyframe (some WebM recordings otherwise stay in seeking state).
      if (maxWatched.current === 0) player.load();
      else player.currentTime = maxWatched.current;
      callbacks.current.onMessage('Watch the course in sequence. You can replay any section you have already watched.');
    }
    lastPosition.current = player.currentTime;
  };
  const finish = () => {
    const player = video.current;
    // The final timeupdate can arrive after the browser sets paused=true.
    if (player && !player.seeking && document.visibilityState === 'visible' && player.currentTime <= lastPosition.current + 2) {
      maxWatched.current = Math.max(maxWatched.current, player.currentTime);
      lastPosition.current = player.currentTime;
    }
    persist();
  };
  const configured = course.video_configured && Boolean(course.video_url);
  const percent = course.video_duration_seconds > 0 ? Math.min(100, Math.round(progress.watched_seconds / course.video_duration_seconds * 100)) : 0;

  return <div className="course-player-layout">
    <section className="learning-card course-video-card" aria-label="Course recording">
      <div className="learning-card-heading"><div><span className="eyebrow">Current course</span><h2>{course.title}</h2></div><span className="learning-badge">{progress.course_completed ? 'Completed' : 'In progress'}</span></div>
      {configured ? <div className="course-video-wrap">
        <video ref={video} controls playsInline preload="metadata" aria-label={`${course.title} course video`}
          src={videoUrl} onPlay={() => { void start(); }} onTimeUpdate={track}
          onSeeking={seek} onPause={persist} onEnded={finish}
          onLoadedMetadata={() => { if (video.current && progressRef.current.watched_seconds > 0) video.current.currentTime = progressRef.current.watched_seconds; }}
          onRateChange={() => { if (video.current) video.current.playbackRate = 1; }}
          onError={() => callbacks.current.onMessage('The course recording could not be loaded. Check that the configured URL points to a playable video file.')} />
        {connecting && <span className="course-video-status" role="status">Connecting to course…</span>}
      </div> : <div className="course-video-empty"><PlayCircle size={36} aria-hidden="true" /><h3>Course recording not configured</h3><p>The course video will appear here once the recording URL is connected.</p></div>}
      <div className="course-watch-progress"><div><span>Course progress</span><strong>{progress.course_completed ? 'Complete' : `${percent}%`}</strong></div><progress value={progress.course_completed ? 100 : percent} max={100} aria-label="Course watch progress" /><span>{clock(progress.watched_seconds)} watched{course.video_duration_seconds > 0 ? ` / ${clock(course.video_duration_seconds)}` : ''}</span></div>
    </section>
    <aside className="learning-card course-outline" aria-label="Course content"><div className="learning-card-heading"><h2><BookOpen size={18} aria-hidden="true" /> Course content</h2></div><div className="course-outline-scroll"><p>{course.description}</p>
      {course.release_id && <div className="course-release-record"><strong>Approved release{course.release_version ? ` · v${course.release_version}` : ''}</strong>{course.approved_by && <span>Reviewed by {course.approved_by}</span>}{course.approval_note && <p>{course.approval_note}</p>}</div>}
      {course.knowledge_units.map((unit, index) => <article key={typeof unit.id === 'string' ? unit.id : index} className="course-knowledge-unit"><span className="eyebrow">{String(index + 1).padStart(2, '0')}</span><h3>{typeof unit.action === 'string' ? unit.action : 'Course observation'}</h3>{(['cue', 'interpretation', 'decision', 'exception'] as const).map(field => typeof unit[field] === 'string' && unit[field] ? <p key={field}><strong>{field === 'cue' ? 'Observe' : field === 'interpretation' ? 'Understand' : field === 'decision' ? 'Decide' : 'If uncertain'}</strong> {unit[field]}</p> : null)}</article>)}
    </div></aside>
  </div>;
}
