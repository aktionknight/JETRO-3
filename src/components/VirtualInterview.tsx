import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Mic, MicOff, Phone, PhoneOff, Send, Volume2 } from 'lucide-react';

interface VirtualInterviewProps {
  token: string;
  onEnd?: (duration: number) => void;
}

type Status =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'speaking'
  | 'listening'
  | 'thinking'
  | 'evaluating'
  | 'ended'
  | 'error';

interface TranscriptEntry {
  role: 'model' | 'user';
  text: string;
}

interface SpeechRecognizer {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((event: { results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  abort(): void;
}

// SpeechRecognition is not included in TypeScript's DOM declarations.
const speechWindow = window as Window & {
  SpeechRecognition?: new () => SpeechRecognizer;
  webkitSpeechRecognition?: new () => SpeechRecognizer;
  webkitAudioContext?: typeof AudioContext;
};
const SpeechRecognitionAPI = speechWindow.SpeechRecognition || speechWindow.webkitSpeechRecognition;

export default function VirtualInterview({ token, onEnd }: VirtualInterviewProps) {
  const [status, setStatus] = useState<Status>('idle');
  const [error, setError] = useState('');
  const [remaining, setRemaining] = useState(120);
  const [elapsed, setElapsed] = useState(0);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [audioLevel, setAudioLevel] = useState(0);
  const [isMuted, setIsMuted] = useState(false);
  const [interimSpeech, setInterimSpeech] = useState('');
  const [typedInput, setTypedInput] = useState('');
  const [captureRevision, setCaptureRevision] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const playbackContextRef = useRef<AudioContext | null>(null);
  const playbackQueueRef = useRef<Float32Array[]>([]);
  const isPlayingRef = useRef(false);
  const playbackGenerationRef = useRef(0);
  const activeSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const finishSourceRef = useRef<(() => void) | null>(null);
  const mountedRef = useRef(false);
  const desiredStatusRef = useRef<Status>('idle');
  const mutedRef = useRef(false);
  const finalizedAnswerRef = useRef('');
  const interimAnswerRef = useRef('');
  const answerRef = useRef('');
  const recognitionRef = useRef<SpeechRecognizer | null>(null);
  const interimTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const restartTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll transcript
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript, interimSpeech]);

  const stopRecognition = useCallback((owner = recognitionRef.current) => {
    if (owner && recognitionRef.current !== owner) return;
    recognitionRef.current = null;
    if (interimTimeoutRef.current) {
      clearTimeout(interimTimeoutRef.current);
      interimTimeoutRef.current = null;
    }
    if (restartTimeoutRef.current) {
      clearTimeout(restartTimeoutRef.current);
      restartTimeoutRef.current = null;
    }
    if (owner) {
      owner.onend = null;
      owner.onresult = null;
      owner.onerror = null;
      try {
        owner.abort();
      } catch {
        // The browser may already have ended this recognizer.
      }
    }
  }, []);

  const canListen = useCallback(() => (
    mountedRef.current && wsRef.current?.readyState === WebSocket.OPEN &&
    desiredStatusRef.current === 'listening' && !mutedRef.current && !isPlayingRef.current
  ), []);

  const stopAudio = useCallback(() => {
    stopRecognition();
    playbackGenerationRef.current += 1;
    const source = activeSourceRef.current;
    activeSourceRef.current = null;
    if (source) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // The source may have finished before teardown.
      }
    }
    finishSourceRef.current?.();
    finishSourceRef.current = null;
    const context = playbackContextRef.current;
    playbackContextRef.current = null;
    context?.close().catch(() => { });
    playbackQueueRef.current = [];
    isPlayingRef.current = false;
  }, [stopRecognition]);

  const sendAnswer = useCallback((textToSend: string, automatic = false) => {
    const text = textToSend.trim();
    const ws = wsRef.current;
    if (!text || !mountedRef.current || !ws || ws.readyState !== WebSocket.OPEN ||
      desiredStatusRef.current !== 'listening' || isPlayingRef.current ||
      (automatic && !canListen())) return;

    desiredStatusRef.current = 'thinking';
    stopRecognition();
    setStatus('thinking');
    try {
      ws.send(JSON.stringify({ type: 'user_turn', text }));
    } catch {
      desiredStatusRef.current = 'listening';
      finalizedAnswerRef.current = answerRef.current;
      interimAnswerRef.current = '';
      setCaptureRevision(revision => revision + 1);
      setStatus('listening');
      return;
    }
    finalizedAnswerRef.current = '';
    interimAnswerRef.current = '';
    answerRef.current = '';
    setInterimSpeech('');
    setTypedInput('');
  }, [canListen, stopRecognition]);

  // Claim playback before any await so a following listening event cannot open capture.
  const playAudioChunk = useCallback(async (base64Data: string) => {
    if (!mountedRef.current || ['evaluating', 'ended', 'error'].includes(desiredStatusRef.current)) return;
    const alreadyPlaying = isPlayingRef.current;
    isPlayingRef.current = true;
    stopRecognition();
    setStatus('speaking');
    const generation = playbackGenerationRef.current;
    try {
      const binary = atob(base64Data);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const samples = new Int16Array(bytes.buffer);
      const decoded = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) decoded[i] = samples[i] / 32768;
      playbackQueueRef.current.push(decoded);
      if (alreadyPlaying) return;

      if (!playbackContextRef.current || playbackContextRef.current.state === 'closed') {
        const AudioCtx = window.AudioContext || speechWindow.webkitAudioContext!;
        playbackContextRef.current = new AudioCtx({ sampleRate: 24000 });
      }
      const ctx = playbackContextRef.current;
      if (ctx.state === 'suspended') await ctx.resume();
      while (mountedRef.current && generation === playbackGenerationRef.current && playbackQueueRef.current.length) {
        const samplesToPlay = playbackQueueRef.current.shift()!;
        const buffer = ctx.createBuffer(1, samplesToPlay.length, 24000);
        buffer.getChannelData(0).set(samplesToPlay);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        activeSourceRef.current = source;
        await new Promise<void>((resolve) => {
          finishSourceRef.current = resolve;
          source.onended = () => resolve();
          source.start();
        });
        if (generation !== playbackGenerationRef.current) return;
        activeSourceRef.current = null;
        finishSourceRef.current = null;
      }
    } catch {
      if (generation === playbackGenerationRef.current) {
        playbackQueueRef.current = [];
        setError('Examiner audio could not play. Read the question and speak or type your answer.');
      }
    } finally {
      if (!alreadyPlaying && mountedRef.current && generation === playbackGenerationRef.current) {
        isPlayingRef.current = false;
        activeSourceRef.current = null;
        finishSourceRef.current = null;
        setStatus(desiredStatusRef.current);
        // React may batch speaking and listening into an unchanged displayed status.
        if (canListen()) setCaptureRevision(revision => revision + 1);
      }
    }
  }, [canListen, stopRecognition]);

  useEffect(() => {
    if (!canListen()) return;
    if (!SpeechRecognitionAPI) {
      setError('Voice recognition is unavailable in this browser. Type your answer below.');
      return;
    }
    const recognition = new SpeechRecognitionAPI();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    recognitionRef.current = recognition;
    let prefix = finalizedAnswerRef.current;
    let restartAllowed = true;
    let failures = 0;
    const ownsCapture = () => recognitionRef.current === recognition && canListen();
    interimAnswerRef.current = '';
    answerRef.current = prefix;
    setInterimSpeech(prefix);
    const restart = () => {
      restartTimeoutRef.current = null;
      if (!restartAllowed || !ownsCapture()) return;
      try {
        recognition.start();
      } catch {
        if (++failures < 3) restartTimeoutRef.current = setTimeout(restart, 300);
        else {
          setError('Microphone could not restart. Toggle the microphone to retry, or type your answer.');
          stopRecognition(recognition);
        }
      }
    };

    recognition.onresult = (event) => {
      if (!ownsCapture()) return;
      failures = 0;
      const finals: string[] = [];
      const interim: string[] = [];
      for (let i = 0; i < event.results.length; i++) {
        (event.results[i].isFinal ? finals : interim).push(event.results[i][0].transcript.trim());
      }
      finalizedAnswerRef.current = [prefix, ...finals].filter(Boolean).join(' ');
      interimAnswerRef.current = interim.filter(Boolean).join(' ');
      answerRef.current = [finalizedAnswerRef.current, interimAnswerRef.current].filter(Boolean).join(' ');
      setInterimSpeech(answerRef.current);
      clearTimeout(interimTimeoutRef.current ?? undefined);
      interimTimeoutRef.current = null;
      if (answerRef.current.length > 10) {
        interimTimeoutRef.current = setTimeout(() => {
          interimTimeoutRef.current = null;
          if (ownsCapture()) sendAnswer(answerRef.current, true);
        }, 1800);
      }
    };
    recognition.onerror = (event) => {
      if (!ownsCapture()) return;
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        restartAllowed = false;
        setError('Microphone permission was denied. Allow microphone access, then toggle the microphone to retry.');
        stopRecognition(recognition);
      } else if (event.error === 'audio-capture') {
        restartAllowed = false;
        setError('No microphone input is available. Check your microphone and toggle it to retry.');
        stopRecognition(recognition);
      } else if (event.error !== 'no-speech' && event.error !== 'aborted' && ++failures >= 3) {
        restartAllowed = false;
        setError('Speech recognition is unavailable. Toggle the microphone to retry, or type your answer.');
        stopRecognition(recognition);
      }
    };
    recognition.onend = () => {
      if (!restartAllowed || !ownsCapture()) return;
      prefix = finalizedAnswerRef.current;
      interimAnswerRef.current = '';
      answerRef.current = prefix;
      setInterimSpeech(prefix);
      restart();
    };
    restart();
    return () => stopRecognition(recognition);
  }, [status, isMuted, captureRevision, canListen, sendAnswer, stopRecognition]);

  // Audio wave visualizer animation
  useEffect(() => {
    let frameId: number;
    const animate = () => {
      if (status === 'speaking') {
        setAudioLevel(0.4 + Math.random() * 0.4);
      } else if (status === 'listening' && interimSpeech) {
        setAudioLevel(0.3 + Math.random() * 0.5);
      } else {
        setAudioLevel(0.05);
      }
      frameId = requestAnimationFrame(animate);
    };
    frameId = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(frameId);
  }, [status, interimSpeech]);

  const startInterview = useCallback(() => {
    const previous = wsRef.current;
    wsRef.current = null;
    previous?.close();
    desiredStatusRef.current = 'connecting';
    stopAudio();
    setStatus('connecting');
    setError('');
    setTranscript([]);
    finalizedAnswerRef.current = '';
    interimAnswerRef.current = '';
    answerRef.current = '';
    setInterimSpeech('');
    setTypedInput('');

    const generation = playbackGenerationRef.current;
    try {
      // Unlock output during the gesture; unavailable audio must not block text interviewing.
      const AudioCtx = window.AudioContext || speechWindow.webkitAudioContext!;
      const context = new AudioCtx({ sampleRate: 24000 });
      playbackContextRef.current = context;
      void context.resume().catch(() => {
        if (mountedRef.current && generation === playbackGenerationRef.current) {
          setError('Examiner audio could not play. Read the question and speak or type your answer.');
        }
      });
    } catch {
      setError('Examiner audio could not play. Read the question and speak or type your answer.');
    }

    try {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/api/v1/learning/interview/live?token=${encodeURIComponent(token)}`;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      const current = () => mountedRef.current && wsRef.current === ws;
      ws.onmessage = (event) => {
        if (!current()) return;
        try {
          const msg = JSON.parse(event.data);
          switch (msg.type) {
            case 'status':
              if (['ended', 'error'].includes(desiredStatusRef.current) ||
                (desiredStatusRef.current === 'evaluating' && !['ended', 'error'].includes(msg.status))) break;
              desiredStatusRef.current = msg.status as Status;
              if (['evaluating', 'ended', 'error'].includes(msg.status)) stopAudio();
              setStatus(isPlayingRef.current ? 'speaking' : desiredStatusRef.current);
              if (msg.status === 'ended') onEnd?.(msg.duration || elapsed);
              break;
            case 'audio':
              void playAudioChunk(msg.data);
              break;
            case 'transcript':
              setTranscript((prev) => {
                const last = prev[prev.length - 1];
                if (last && last.role === msg.role) {
                  return [...prev.slice(0, -1), { ...last, text: last.text + ' ' + msg.text }];
                }
                return [...prev, { role: msg.role, text: msg.text }];
              });
              break;
            case 'timer':
              setRemaining(msg.remaining);
              setElapsed(msg.elapsed);
              break;
            case 'error':
              desiredStatusRef.current = 'error';
              stopAudio();
              setError(msg.message);
              setStatus('error');
              break;
            case 'voice_error':
              setError(msg.message);
              break;
          }
        } catch {
          // Ignore malformed server events.
        }
      };
      ws.onerror = () => {
        if (!current()) return;
        desiredStatusRef.current = 'error';
        stopAudio();
        setError('Connection to live interview service failed.');
        setStatus('error');
      };
      ws.onclose = () => {
        if (!current()) return;
        if (!['ended', 'error'].includes(desiredStatusRef.current)) desiredStatusRef.current = 'ended';
        stopAudio();
        setStatus(desiredStatusRef.current);
      };
    } catch (err) {
      desiredStatusRef.current = 'error';
      stopAudio();
      setError(err instanceof Error ? err.message : 'Failed to connect to interview');
      setStatus('error');
    }
  }, [token, stopAudio, playAudioChunk, onEnd, elapsed]);

  const endInterview = useCallback(() => {
    desiredStatusRef.current = 'evaluating';
    stopAudio();
    setStatus('evaluating');
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: 'end' }));
      } catch {
        // Socket closure still performs local teardown.
      }
    }
  }, [stopAudio]);

  const toggleMute = () => {
    mutedRef.current = !mutedRef.current;
    if (mutedRef.current) stopRecognition();
    setIsMuted(mutedRef.current);
    setError('');
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const ws = wsRef.current;
      wsRef.current = null;
      stopAudio();
      ws?.close();
    };
  }, [stopAudio]);

  const formatTime = (seconds: number) => {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
  };

  const progress = Math.min(100, (elapsed / 120) * 100);

  return (
    <div className="vi-container">
      {/* Header */}
      <div className="vi-header">
        <div className="vi-header-left">
          <div className="vi-indicator-dot" data-status={status} />
          <span className="vi-status-text">
            {status === 'idle' && 'Ready to start'}
            {status === 'connecting' && 'Connecting to examiner…'}
            {status === 'connected' && 'Connected'}
            {status === 'speaking' && 'Examiner speaking…'}
            {status === 'listening' && 'Listening to you (speak now)'}
            {status === 'thinking' && 'Examiner evaluating…'}
            {status === 'evaluating' && 'Preparing and saving your evaluation report…'}
            {status === 'ended' && 'Interview complete'}
            {status === 'error' && 'Connection error'}
          </span>
        </div>
        <div className="vi-timer">
          <span className="vi-timer-elapsed">{formatTime(elapsed)}</span>
          <span className="vi-timer-sep">/</span>
          <span className="vi-timer-total">{formatTime(120)}</span>
        </div>
      </div>

      {/* Progress bar */}
      <div className="vi-progress-bar">
        <div className="vi-progress-fill" style={{ width: `${progress}%` }} />
      </div>

      {/* Main area */}
      <div className="vi-main">
        {status === 'idle' ? (
          <div className="vi-start-screen">
            <div className="vi-start-icon">
              <Phone size={40} />
            </div>
            <h3>Live AI Technical Viva</h3>
            <p>
              A 2-minute live oral examination evaluating your understanding of the shaft and journal bearing inspection.
            </p>
            <ul>
              <li>
                <strong>Grounded:</strong> Questions come directly from the expert video recording and approved knowledge units.
              </li>
              <li>
                <strong>Spoken Voice:</strong> The examiner speaks naturally via Gemini TTS and waits for your verbal response.
              </li>
              <li>
                <strong>2-Minute Limit:</strong> Focused viva covering observation, measurement tolerances, and corrective decisions.
              </li>
            </ul>
            <button className="vi-start-btn" onClick={startInterview}>
              <Phone size={18} />
              Start Live Interview
            </button>
          </div>
        ) : status === 'ended' ? (
          <div className="vi-end-screen">
            <div className="vi-end-icon">
              <CheckCircle2 size={42} style={{ color: '#10b981' }} />
            </div>
            <h3>Interview Completed</h3>
            <p>
              Duration: <strong>{formatTime(elapsed)}</strong>. Your saved evaluation report appears below.
            </p>
            {transcript.length > 0 && (
              <div className="vi-end-transcript">
                <h4>Viva Transcript</h4>
                {transcript.map((entry, i) => (
                  <div key={i} className={`vi-transcript-entry vi-role-${entry.role}`}>
                    <span className="vi-role-label">{entry.role === 'model' ? 'Examiner' : 'Apprentice'}</span>
                    <p>{entry.text}</p>
                  </div>
                ))}
              </div>
            )}
            <button className="vi-start-btn" onClick={startInterview}>
              <Phone size={18} />
              Retake Interview
            </button>
          </div>
        ) : (
          <div className="vi-active">
            {/* Visualisation Area */}
            <div className="vi-viz">
              <div className="vi-avatar" data-status={status}>
                {status === 'speaking' ? (
                  <Volume2 size={36} className="vi-avatar-icon" />
                ) : (
                  <Mic size={36} className="vi-avatar-icon" />
                )}
                {/* Pulse rings */}
                <div
                  className="vi-pulse-ring"
                  style={{
                    transform: `scale(${1 + audioLevel * 0.8})`,
                    opacity: status === 'speaking' || status === 'listening' ? 0.6 : 0.2,
                  }}
                />
                <div
                  className="vi-pulse-ring vi-pulse-ring-2"
                  style={{
                    transform: `scale(${1 + audioLevel * 1.3})`,
                    opacity: status === 'speaking' || status === 'listening' ? 0.35 : 0.1,
                  }}
                />
              </div>

              {/* Dynamic waveform */}
              <div className="vi-waveform">
                {Array.from({ length: 32 }, (_, i) => {
                  const height =
                    status === 'speaking'
                      ? 10 + Math.sin(Date.now() / 150 + i * 0.4) * 22
                      : status === 'listening' && interimSpeech
                        ? 8 + audioLevel * 35 + Math.sin(Date.now() / 200 + i * 0.8) * 10
                        : status === 'listening'
                          ? 6 + Math.sin(Date.now() / 400 + i * 0.2) * 4
                          : 4;
                  return <div key={i} className="vi-wave-bar" style={{ height: `${height}px` }} />;
                })}
              </div>
            </div>

            {/* Live transcript log */}
            <div className="vi-live-transcript">
              {transcript.map((entry, i) => (
                <div key={i} className={`vi-transcript-entry vi-role-${entry.role}`}>
                  <span className="vi-role-label">{entry.role === 'model' ? 'Examiner' : 'You'}</span>
                  <p>{entry.text}</p>
                </div>
              ))}
              {interimSpeech && (
                <div className="vi-transcript-entry vi-role-user vi-interim">
                  <span className="vi-role-label">You (Speaking…)</span>
                  <p>{interimSpeech}</p>
                </div>
              )}
              <div ref={transcriptEndRef} />
            </div>

            {/* Input & Voice Controls */}
            <div className="vi-controls-wrapper">
              {/* Optional text input box for keyboard response */}
              <form
                className="vi-text-input-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (typedInput.trim()) {
                    sendAnswer(typedInput);
                  }
                }}
              >
                <input
                  type="text"
                  placeholder={
                    status === 'listening'
                      ? 'Speak into microphone or type your answer here…'
                      : 'Examiner is responding…'
                  }
                  value={typedInput}
                  onChange={(e) => setTypedInput(e.target.value)}
                  disabled={status !== 'listening'}
                  className="vi-text-field"
                />
                <button
                  type="submit"
                  disabled={status !== 'listening' || (!typedInput.trim() && !interimSpeech.trim())}
                  className="vi-send-btn"
                  title="Submit answer"
                >
                  <Send size={18} />
                </button>
              </form>

              {/* Action buttons */}
              <div className="vi-controls">
                <button
                  className={`vi-control-btn ${isMuted ? 'vi-muted' : ''}`}
                  onClick={toggleMute}
                  title={isMuted ? 'Unmute microphone' : 'Mute microphone'}
                >
                  {isMuted ? <MicOff size={20} /> : <Mic size={20} />}
                </button>
                {interimSpeech && (
                  <button
                    className="vi-control-btn vi-done-speaking-btn"
                    onClick={() => sendAnswer(interimSpeech)}
                    title="Send what you said"
                  >
                    Done Speaking
                  </button>
                )}
                <button className="vi-control-btn vi-end-btn" onClick={endInterview} title="End interview">
                  <PhoneOff size={20} />
                  <span>End</span>
                </button>
              </div>
            </div>
          </div>
        )}

        {error && (
          <div className="vi-error">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}
      </div>
    </div>
  );
}
