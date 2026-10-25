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

// Support SpeechRecognition across browsers
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const SpeechRecognitionAPI: any =
  typeof window !== 'undefined'
    ? // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    : null;

export default function VirtualInterview({ token, onEnd }: VirtualInterviewProps) {
  const [status, storeStatus] = useState<Status>('idle');
  const statusRef = useRef<Status>('idle');
  const mutedRef = useRef(false);
  const setStatus = useCallback((next: Status | ((current: Status) => Status)) => {
    const value = typeof next === 'function' ? next(statusRef.current) : next;
    statusRef.current = value;
    storeStatus(value);
  }, []);
  const [error, setError] = useState('');
  const [remaining, setRemaining] = useState(120);
  const [elapsed, setElapsed] = useState(0);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [audioLevel, setAudioLevel] = useState(0);
  const [isMuted, setIsMuted] = useState(false);
  const [interimSpeech, setInterimSpeech] = useState('');
  const [typedInput, setTypedInput] = useState('');

  const wsRef = useRef<WebSocket | null>(null);
  const playbackContextRef = useRef<AudioContext | null>(null);
  const playbackQueueRef = useRef<ArrayBuffer[]>([]);
  const isPlayingRef = useRef(false);
  const playbackEpochRef = useRef(0);
  const playbackSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const finishPlaybackRef = useRef<(() => void) | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const recognitionRef = useRef<any>(null);
  const interimTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement>(null);
  const animFrameRef = useRef<number>(0);

  // Auto-scroll transcript
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [transcript, interimSpeech]);

  // Clean up audio
  const stopAudio = useCallback(() => {
    playbackEpochRef.current += 1;
    if (interimTimeoutRef.current) {
      clearTimeout(interimTimeoutRef.current);
      interimTimeoutRef.current = null;
    }
    if (playbackSourceRef.current) {
      try { playbackSourceRef.current.stop(); } catch { /* Already stopped. */ }
      playbackSourceRef.current = null;
    }
    finishPlaybackRef.current?.();
    finishPlaybackRef.current = null;
    if (recognitionRef.current) {
      recognitionRef.current.onend = null;
      recognitionRef.current.onresult = null;
      recognitionRef.current.onerror = null;
      try {
        recognitionRef.current.stop();
      } catch {
        // ignore
      }
      recognitionRef.current = null;
    }
    if (playbackContextRef.current) {
      playbackContextRef.current.close().catch(() => {});
      playbackContextRef.current = null;
    }
    if (animFrameRef.current) {
      cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = 0;
    }
    playbackQueueRef.current = [];
    isPlayingRef.current = false;
  }, []);

  // Send apprentice turn to backend
  const sendAnswer = useCallback((textToSend: string) => {
    const trimmed = textToSend.trim();
    if (!trimmed || statusRef.current !== 'listening' || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    setStatus('thinking');
    if (recognitionRef.current) {
      recognitionRef.current.onend = null;
      try {
        recognitionRef.current.stop();
      } catch {
        // ignore
      }
    }
    if (interimTimeoutRef.current) {
      clearTimeout(interimTimeoutRef.current);
      interimTimeoutRef.current = null;
    }

    setInterimSpeech('');
    setTypedInput('');

    wsRef.current.send(JSON.stringify({ type: 'user_turn', text: trimmed }));
  }, [setStatus]);

  // Keep the microphone closed until the entire examiner turn has played.
  const playAudioChunk = useCallback(async (base64Data: string) => {
    const epoch = playbackEpochRef.current;
    let ownsPlayback = false;
    try {
      const binary = atob(base64Data);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const samples = new Int16Array(bytes.buffer);
      const floats = new Float32Array(samples.length);
      for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 32768;
      playbackQueueRef.current.push(floats.buffer);
      if (isPlayingRef.current) return;
      ownsPlayback = true;
      isPlayingRef.current = true;
      setStatus('speaking');
      if (!playbackContextRef.current || playbackContextRef.current.state === 'closed') {
        const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
        playbackContextRef.current = new AudioCtx({ sampleRate: 24000 });
      }
      const ctx = playbackContextRef.current;
      if (ctx.state === 'suspended') await ctx.resume();
      while (epoch === playbackEpochRef.current && playbackQueueRef.current.length) {
        const data = new Float32Array(playbackQueueRef.current.shift()!);
        const buffer = ctx.createBuffer(1, data.length, 24000);
        buffer.getChannelData(0).set(data);
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        playbackSourceRef.current = source;
        await new Promise<void>(resolve => {
          finishPlaybackRef.current = resolve;
          source.onended = () => resolve();
          source.start();
        });
        if (playbackSourceRef.current === source) playbackSourceRef.current = null;
      }
    } catch {
      if (epoch === playbackEpochRef.current) {
        if (ownsPlayback) playbackQueueRef.current = [];
        setError('Examiner audio could not play. Read the question and speak or type your answer.');
      }
    } finally {
      if (ownsPlayback && epoch === playbackEpochRef.current) {
        finishPlaybackRef.current = null;
        isPlayingRef.current = false;
        if (statusRef.current === 'speaking') setStatus('listening');
      }
    }
  }, [setStatus]);

  // Each listening turn owns its recognizer; disposed callbacks cannot restart it.
  useEffect(() => {
    if (status !== 'listening' || isMuted) return;
    if (!SpeechRecognitionAPI) {
      setError('Voice recognition is unavailable in this browser. Type your answer below.');
      return;
    }
    const recognition = new SpeechRecognitionAPI();
    recognitionRef.current = recognition;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    let disposed = false;
    let retryAllowed = true;
    let failures = 0;
    let prefix = '';
    let latestText = '';
    let restartTimer: ReturnType<typeof setTimeout> | null = null;
    const active = () => !disposed && recognitionRef.current === recognition
      && statusRef.current === 'listening' && !mutedRef.current;
    const restart = () => {
      if (!active() || !retryAllowed) return;
      try { recognition.start(); }
      catch {
        if (++failures < 3) restartTimer = setTimeout(restart, 300);
        else setError('Microphone could not restart. Toggle the microphone to retry, or type your answer.');
      }
    };
    recognition.onresult = (event: any) => {
      if (!active()) return;
      // resultIndex is only the changed suffix; rebuild all results to retain earlier words.
      let text = '';
      for (let index = 0; index < event.results.length; index++) {
        text += event.results[index][0].transcript + ' ';
      }
      latestText = `${prefix} ${text}`.trim();
      setInterimSpeech(latestText);
      failures = 0;
      if (interimTimeoutRef.current) clearTimeout(interimTimeoutRef.current);
      if (latestText) {
        interimTimeoutRef.current = setTimeout(() => {
          if (active()) sendAnswer(latestText);
        }, 1800);
      }
    };
    recognition.onerror = (event: any) => {
      if (!active()) return;
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
        retryAllowed = false;
        setError('Microphone permission was denied. Allow microphone access, then toggle the microphone to retry.');
      } else if (event.error === 'audio-capture') {
        retryAllowed = false;
        setError('No microphone input is available. Check your microphone and toggle it to retry.');
      } else if (event.error !== 'no-speech' && event.error !== 'aborted' && ++failures >= 3) {
        retryAllowed = false;
        setError('Speech recognition is unavailable. Toggle the microphone to retry, or type your answer.');
      }
    };
    recognition.onend = () => {
      if (!active() || !retryAllowed) return;
      prefix = latestText;
      restartTimer = setTimeout(restart, 250);
    };
    restart();
    return () => {
      disposed = true;
      if (restartTimer) clearTimeout(restartTimer);
      if (interimTimeoutRef.current) {
        clearTimeout(interimTimeoutRef.current);
        interimTimeoutRef.current = null;
      }
      recognition.onend = null;
      recognition.onresult = null;
      recognition.onerror = null;
      try { recognition.abort(); } catch { /* Already stopped. */ }
      if (recognitionRef.current === recognition) recognitionRef.current = null;
    };
  }, [status, isMuted, sendAnswer]);

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

  // Start interview session
  const startInterview = useCallback(async () => {
    stopAudio();
    setStatus('connecting');
    setError('');
    setTranscript([]);
    setInterimSpeech('');
    setTypedInput('');
    playbackQueueRef.current = [];

    try {
      // Resume output from the Start click rather than a later WebSocket callback.
      const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      playbackContextRef.current = new AudioCtx({ sampleRate: 24000 });
      await playbackContextRef.current.resume();
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${protocol}//${window.location.host}/api/v1/learning/interview/live?token=${encodeURIComponent(token)}`;
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) return;
        try {
          const msg = JSON.parse(event.data);
          switch (msg.type) {
            case 'status':
              if (msg.status === 'ended') {
                setStatus('ended');
                stopAudio();
                onEnd?.(msg.duration || elapsed);
              } else if (msg.status !== 'listening' || !isPlayingRef.current) {
                setStatus(msg.status as Status);
              }
              break;
            case 'audio':
              playAudioChunk(msg.data);
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
              setError(msg.message);
              setStatus('error');
              stopAudio();
              break;
          }
        } catch {
          // ignore
        }
      };

      ws.onerror = () => {
        if (wsRef.current !== ws) return;
        setError('Connection to live interview service failed.');
        setStatus('error');
        stopAudio();
      };

      ws.onclose = () => {
        if (wsRef.current !== ws) return;
        setStatus((cur) => (cur !== 'ended' && cur !== 'error' ? 'ended' : cur));
        stopAudio();
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to connect to interview');
      setStatus('error');
      stopAudio();
    }
  }, [token, stopAudio, playAudioChunk, onEnd, elapsed]);

  const endInterview = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'end' }));
    }
    setStatus('evaluating');
    stopAudio();
  }, [stopAudio]);

  const toggleMute = () => {
    mutedRef.current = !mutedRef.current;
    setIsMuted(mutedRef.current);
    setError('');
  };

  useEffect(() => {
    return () => {
      stopAudio();
      if (wsRef.current) {
        wsRef.current.close();
      }
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
