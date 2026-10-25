"""Pure voice event tests written before protocol normalization."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.voice_protocol import normalize_event, voice_session_config, question_response

class VoiceProtocolTests(unittest.TestCase):
    def test_completed_transcript_uses_semantic_item_id(self):
        event=normalize_event({'type':'conversation.item.input_audio_transcription.completed','event_id':'transport','item_id':'utterance','transcript':'My reasoning'})
        self.assertEqual(event,{'kind':'answer','id':'utterance','text':'My reasoning'})
    def test_partial_caption_cannot_be_an_answer(self):
        self.assertIsNone(normalize_event({'type':'conversation.item.input_audio_transcription.delta','delta':'partial'}))
    def test_playback_completion_distinct_from_generation_completion(self):
        self.assertIsNone(normalize_event({'type':'response.done'}))
        self.assertEqual(normalize_event({'type':'output_audio_buffer.stopped'})['kind'],'delivered')
    def test_interruption_has_own_event(self):
        self.assertEqual(normalize_event({'type':'input_audio_buffer.speech_started','item_id':'utterance'}),{'kind':'interruption','item_id':'utterance'})
    def test_automatic_responses_disabled(self):
        config=voice_session_config('gpt-realtime')
        self.assertFalse(config['audio']['input']['turn_detection']['create_response'])
        self.assertNotIn('tools',config)
    def test_question_response_contains_only_reviewed_question(self):
        value=question_response('Explain the check')
        self.assertEqual(value['type'],'response.create')
        self.assertIn('Explain the check',value['response']['instructions'])
    def test_empty_or_oversized_utterance_rejected(self):
        for value in ('', ' '*10, 'x'*10001):
            self.assertIsNone(normalize_event({'type':'conversation.item.input_audio_transcription.completed','item_id':'u','transcript':value}))

    def test_transport_replays_keep_semantic_identity(self):
        for kind in ('input_audio_buffer.speech_started','conversation.item.input_audio_transcription.completed'):
            first=normalize_event({'type':kind,'event_id':'transport-a','item_id':'utterance','transcript':'My reasoning'})
            replay=normalize_event({'type':kind,'event_id':'transport-b','item_id':'utterance','transcript':'My reasoning'})
            self.assertEqual(first,replay)

    def test_invalid_semantic_item_ids_are_rejected(self):
        for kind in ('input_audio_buffer.speech_started','conversation.item.input_audio_transcription.completed'):
            for identity in (None,0,False,[],{},'', 'x'*129):
                with self.subTest(kind=kind,identity=identity):
                    self.assertIsNone(normalize_event({'type':kind,'item_id':identity,'transcript':'My reasoning'}))
            self.assertIsNone(normalize_event({'type':kind,'event_id':'transport-only','transcript':'My reasoning'}))
            self.assertIsNotNone(normalize_event({'type':kind,'item_id':'x'*128,'transcript':'My reasoning'}))

if __name__=='__main__':unittest.main()
