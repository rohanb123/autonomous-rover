import { useRef, useState } from 'react';
import { brain } from '../api/brain';

// Push-to-talk: hold to record (MediaRecorder, audio/webm), release to send to the brain,
// which transcribes with Grok (offline backup: faster-whisper) and parses the mission.
export default function VoiceButton({ disabled, onError }: { disabled: boolean; onError(msg: string): void }) {
  const rec = useRef<MediaRecorder | null>(null);
  const [state, setState] = useState<'idle' | 'recording' | 'sending'>('idle');

  const start = async () => {
    if (disabled || state !== 'idle') return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks: Blob[] = [];
      const r = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      r.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      r.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        setState('sending');
        brain.missionVoice(new Blob(chunks, { type: 'audio/webm' }))
          .then(() => onError(''), (e) => onError(String(e.message ?? e)))
          .finally(() => setState('idle'));
      };
      r.start();
      rec.current = r;
      setState('recording');
    } catch (e) {
      onError(`Microphone unavailable: ${String(e)}`);
    }
  };
  const stop = () => rec.current?.state === 'recording' && rec.current.stop();

  const label = state === 'recording' ? '● Listening…' : state === 'sending' ? 'Sending…' : '🎙 Hold to talk';
  return (
    <button className={`voice ${state}`} disabled={disabled || state === 'sending'}
      onMouseDown={start} onMouseUp={stop} onMouseLeave={stop} onTouchStart={start} onTouchEnd={stop}>
      {label}
    </button>
  );
}
