import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from hakimi.voice import tts, tts_worker as worker


class RoleEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.command = self.base/'say'
        self.settings = dict(tts_engine='gpt-sovits',tts_model='melo',
                             tts_role_command=str(self.command),tts_role_revision='warm-v2')

    def helper(self, body):
        self.command.write_text('#!'+sys.executable+'\n'+body)
        self.command.chmod(0o700)

    def wav(self, pcm, rate=32000):
        output=io.BytesIO()
        with wave.open(output,'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        return output.getvalue()

    def test_role_reads_utf8_stdin_and_pcm(self):
        payload=self.wav(bytes.fromhex('004000c0'))
        self.helper("import sys,json\nassert sys.argv[1:] == ['--stdout']\n"
                    "assert json.load(sys.stdin) == {'text':'晚安'}\n"
                    "sys.stdout.buffer.write("+repr(payload)+")\n")
        samples, rate=worker.role_samples('晚安',self.command,3)
        self.assertEqual(rate,32000)
        self.assertEqual(samples.tolist(),[.5,-.5])

    def test_rejects_truncated_or_oversized_pcm(self):
        for payload in (self.wav(b'\x00\x00'*10)[:-4],self.wav(b'\x00\x00'*(27*8000),rate=8000)):
            with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                worker._read_wav(io.BytesIO(payload),max_seconds=26)

    def test_exit_and_timeout_reap_role(self):
        self.helper("raise SystemExit(7)\n")
        with self.assertRaises(subprocess.CalledProcessError):
            worker.role_samples('晚安',self.command,2)
        self.helper("import time\ntime.sleep(10)\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            worker.role_samples('晚安',self.command,.15)

    def test_config_reserves_fallback_time_and_versions_cache(self):
        with patch.object(tts,'settings',return_value=self.settings):
            task=tts.synthesis_task(self.base,'晚安','out.silk',budget=22)
            self.assertEqual(task['role'],{'command':str(self.command),'timeout':12})
            self.assertEqual(task['fallback_model'],'melo')
            self.assertEqual(tts.synthesis_task(self.base,'晚安','out.silk',budget=50)['role']['timeout'],25)
            self.assertEqual(tts.synthesis_task(self.base,'晚安','out.silk',budget=35)['role']['timeout'],25)
            self.assertEqual(tts.synthesis_task(self.base,'晚安','out.silk',budget=25)['role']['timeout'],15)
            self.assertEqual(tts.synthesis_task(self.base,'晚安','out.silk',budget=9)['role']['timeout'],0)
            old_name=tts.generated_name('group:1','turn','晚安',self.base)
            self.settings['tts_role_revision']='next'
            self.assertNotEqual(tts.generated_name('group:1','turn','晚安',self.base),old_name)
            self.settings['tts_engine']='zipvoice'
            self.assertNotIn('role',tts.synthesis_task(self.base,'晚安','out.silk'))

    def run_worker(self, task):
        stdout=io.StringIO()
        with patch.object(sys,'stdin',io.StringIO(json.dumps(task))), contextlib.redirect_stdout(stdout):
            worker.main()
        return json.loads(stdout.getvalue())

    def test_success_encodes_once_with_normal_qq_loudness(self):
        with patch.object(tts,'settings',return_value=self.settings):
            task=tts.synthesis_task(self.base,'晚安',str(self.base/'out.silk'),budget=35)
        samples=[.25]*3200
        with patch.object(worker,'role_samples',return_value=(samples,32000)), \
             patch.object(worker,'_encode',return_value=123) as encode, patch.object(worker,'_vits') as fallback:
            result=self.run_worker(task)
        self.assertEqual(result['backend'],'gpt-sovits')
        self.assertEqual(result['duration'],.1)
        encode.assert_called_once_with(samples,32000,self.base/'out.silk',32000,True)
        fallback.assert_not_called()

    def test_timeout_uses_existing_melo_fallback(self):
        output=self.base/'out.silk'
        output.write_bytes(b'partial')
        with patch.object(tts,'settings',return_value=self.settings):
            task=tts.synthesis_task(self.base,'晚安',str(output),budget=35)
        def fallback(options,path,bitrate):
            self.assertFalse(path.exists())
            self.assertEqual(options['model'],'melo')
            self.assertEqual(bitrate,32000)
            path.write_bytes(b'valid-fallback')
            return {'bytes':14}
        with patch.object(worker,'role_samples',side_effect=subprocess.TimeoutExpired('say',25)), \
             patch.object(worker,'_vits',side_effect=fallback):
            result=self.run_worker(task)
        self.assertEqual(result['backend'],'vits-fallback')
        self.assertEqual(result['role_error'],'TimeoutExpired')
        self.assertEqual(output.read_bytes(),b'valid-fallback')
