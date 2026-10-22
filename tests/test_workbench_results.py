import importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from benchcad_runtime.workbench import (base_run,make_package,validate_package,sync_package,
    finalize_runtime,dump,read,sha)

VIEWS={'record_scope':'final_retained_views','status':'unavailable','views':[]}

class ResultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.package=self.root/'package'
        self.run=base_run(5,'run-one','Fixture',model='chosen-model')
    def make(self):return make_package(self.package,self.run,VIEWS)
    def test_idempotence_and_conflict(self):
        self.make();before=(self.package/'manifest.json').read_bytes();self.make()
        self.assertEqual(before,(self.package/'manifest.json').read_bytes())
        self.run['label']='Changed'
        with self.assertRaisesRegex(ValueError,'conflict'):self.make()
    def test_unlisted_private_file_hash_mismatch_traversal(self):
        self.make();(self.package/'cli.jsonl').write_text('private')
        with self.assertRaisesRegex(ValueError,'unlisted'):validate_package(self.package)
        (self.package/'cli.jsonl').unlink();(self.package/'run.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash'):validate_package(self.package)
        m=read(self.package/'manifest.json');m['files']['../answer.json']={'sha256':'0'*64,'bytes':1}
        (self.package/'manifest.json').write_bytes(dump(m))
        with self.assertRaises(ValueError):validate_package(self.package)
    def test_paths_secrets_and_unsupported_keys_rejected(self):
        self.run['note']='https://example.test/results'
        self.make()  # An HTTPS URL must not be mistaken for a Windows drive.
        for value in ['C:/Users/example/a','/home/example/data','key@example.test','api_key=hidden']:
            self.run['note']=value
            with self.assertRaisesRegex(ValueError,'private_information'):self.make()
        self.run['note']='safe';self.run['raw_log']='not allowed'
        with self.assertRaisesRegex(ValueError,'unsupported'):self.make()
    def test_null_not_zero_and_png_metadata_removed(self):
        from PIL import Image,PngImagePlugin
        path=self.root/'drawing.png';info=PngImagePlugin.PngInfo();info.add_text('source','private path')
        Image.new('RGB',(10,10),'white').save(path,pnginfo=info)
        make_package(self.package,self.run,VIEWS,drawing=path)
        _,r,_=validate_package(self.package);self.assertIsNone(r['metrics']['reconstruction_score_100'])
        with Image.open(self.package/'drawing.png') as img:self.assertFalse(img.info)
    def test_retry_only_command_no_model_or_evaluation(self):
        self.make();cfg=self.root/'sync-config.json';cfg.write_bytes(dump({'command':['trusted-sync','{package}']}))
        hashes={p.name:sha(p.read_bytes()) for p in self.package.iterdir()}
        with patch('benchcad_runtime.workbench.subprocess.run') as command:
            command.side_effect=[type('R',(),{'returncode':1})(),type('R',(),{'returncode':0})()]
            self.assertEqual(sync_package(self.package,cfg)['status'],'pending_retry')
            self.assertEqual(sync_package(self.package,cfg)['status'],'synced')
            self.assertEqual(sync_package(self.package,cfg)['status'],'synced')
            self.assertEqual(command.call_count,2)
            self.assertEqual(command.call_args.args[0],['trusted-sync',str(self.package.resolve())])
        self.assertEqual(hashes,{p.name:sha(p.read_bytes()) for p in self.package.iterdir()})
    def test_changed_target_and_timeout_remain_retryable(self):
        self.make();cfg=self.root/'config.json';cfg.write_bytes(dump({'command':['sync','{package}']}))
        import subprocess
        with patch('benchcad_runtime.workbench.subprocess.run',side_effect=subprocess.TimeoutExpired('sync',1)):
            self.assertEqual(sync_package(self.package,cfg)['status'],'pending_retry')
        with patch('benchcad_runtime.workbench.subprocess.run') as cmd:
            cmd.return_value.returncode=0;sync_package(self.package,cfg)
            cfg.write_bytes(dump({'command':['new-target','{package}']}));sync_package(self.package,cfg)
            self.assertEqual(cmd.call_count,2)
    def test_completion_hook_preserves_result_on_sync_failure(self):
        summary={'model':'fixture','stages':{},'metrics':{'reconstruction_iou':{'status':'not_evaluated','score':None}}}
        cfg=self.root/'config.json';cfg.write_bytes(dump({'command':['sync','{package}']}))
        with patch('benchcad_runtime.workbench.subprocess.run') as cmd:
            cmd.return_value.returncode=1
            result=finalize_runtime(self.root,summary,case_number=5,run_id='hook',config=cfg)
            self.assertEqual(result['status'],'pending_retry');self.assertEqual(cmd.call_count,3)
        validate_package(self.root/'workbench-result')
    def test_receiver_two_cases_duplicate_run_names_and_idempotence(self):
        module_path=Path(__file__).resolve().parents[2]/'benchmark-architecture/sync_results.py'
        if not module_path.exists():self.skipTest('workbench checkout is not installed')
        spec=importlib.util.spec_from_file_location('receiver',module_path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.make();site=self.root/'site';module.ingest(self.package,site);module.ingest(self.package,site)
        self.run['case_number']=13;other=self.root/'second';make_package(other,self.run,VIEWS);module.ingest(other,site)
        self.assertEqual(len(list((site/'result-packages').iterdir())),2)
    def test_failed_export_never_changes_controller_success(self):
        s={'model':'fixture','success':True,'stages':{},'metrics':{'reconstruction_iou':{'status':'not_evaluated','score':None}}}
        result=finalize_runtime(self.root,s,case_number=5,run_id='../invalid')
        self.assertEqual(result['status'],'export_or_sync_failed');self.assertTrue(s['success'])

if __name__=='__main__':unittest.main()
