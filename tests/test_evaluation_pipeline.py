from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch
from benchcad_runtime.artifacts import sha256
from benchcad_runtime.episode import Execution
from benchcad_runtime.docker_executor import DockerLimits
from benchcad_runtime.report import save_summary_and_report
from evaluation.pipeline import complete_evaluation,evaluate_frozen
from evaluation.scoring import VERSION,reconstruction_score
from benchcad_cadquery.cli import parser,configure,summarize

class PipelineTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.bundle=self.root/'bundle';self.bundle.mkdir()
  self.ref=self.root/'input.step';self.ref.write_bytes(b'reference')
  (self.bundle/'model.step').write_bytes(b'candidate')
  (self.bundle/'manifest.json').write_text(json.dumps({'kind':'reconstruction','files':{'model.step':{'sha256':sha256(b'candidate'),'bytes':9}}}))
  self.vendor=self.root/'vendor';(self.vendor/'envs/common').mkdir(parents=True)
  (self.vendor/'envs/common/part_metric.py').write_text('# trusted fixture')
 def result(self,label,score=1):
  return {'status':'computed','metric_version':VERSION,'iou':score,**reconstruction_score(score,.2),
   'reference':{'sha256':sha256(b'reference')},'candidate':{'sha256':sha256(b'reference' if label=='identity' else b'candidate')}}
 def summary(self):
  s=summarize({},1,False,'chosen-model');s['success']=True
  s['stages']['reconstruction']={'submitted':True,'bundle':str(self.bundle),'ended_by':'submitted','decision_rounds':2,'code_executions':2,'wall_seconds':1}
  return s
 def test_real_boundary_snapshots_and_computed_zero(self):
  def execute(command,timeout,directory):
   label=directory.name;w=self.root/'evaluation/workspace'
   self.assertEqual(set(p.name for p in w.iterdir()),{'reference.step','candidate.step'} if label=='identity' else {'reference.step','candidate.step','identity.json'})
   self.assertIn('-I',command);self.assertLessEqual(timeout,300)
   (w/(label+'.json')).write_text(json.dumps(self.result(label,1 if label=='identity' else .3)))
   return Execution(0)
  with patch('evaluation.pipeline.DockerExecutor') as ex:
   ex.return_value.run_command.side_effect=execute
   s=complete_evaluation(self.summary(),self.root,self.ref,self.vendor,'eval:test',DockerLimits())
  self.assertEqual(s['metrics']['reconstruction_iou']['status'],'computed')
  self.assertEqual(s['metrics']['reconstruction_iou']['score'],0)
  self.assertTrue(s['success'])
  save_summary_and_report(self.root,s)
  page=(self.root/'report.html').read_text(encoding='utf-8')
  self.assertIn('已评分',page);self.assertIn('0.00 / 100',page)
  self.assertNotIn('awaiting_independent_AI_reconstruction',json.dumps(s))
 def test_invalid_frozen_hash_fails_before_docker(self):
  (self.bundle/'model.step').write_bytes(b'mutated')
  with patch('evaluation.pipeline.DockerExecutor') as ex:
   s=complete_evaluation(self.summary(),self.root,self.ref,self.vendor,'eval:test',DockerLimits())
   ex.assert_not_called()
  self.assertEqual(s['metrics']['reconstruction_iou']['status'],'failed');self.assertIsNone(s['metrics']['reconstruction_iou']['score'])
  self.assertFalse(s['success']);self.assertTrue(s['generation_success'])
 def test_identity_failure_never_scores_candidate(self):
  calls=[]
  def execute(command,timeout,directory):
   calls.append(directory.name)
   (self.root/'evaluation/workspace/identity.json').write_text(json.dumps(self.result('identity',.5)))
   return Execution(0)
  with patch('evaluation.pipeline.DockerExecutor') as ex:
   ex.return_value.run_command.side_effect=execute
   s=complete_evaluation(self.summary(),self.root,self.ref,self.vendor,'eval:test',DockerLimits())
  self.assertEqual(calls,['identity']);self.assertEqual(s['metrics']['reconstruction_iou']['status'],'failed')
 def test_missing_submission_skips_evaluator_but_writes_report(self):
  s=self.summary();s['success']=False;s['stages']['reconstruction']['submitted']=False
  with patch('evaluation.pipeline.evaluate_frozen') as evaluate:
   complete_evaluation(s,self.root,self.ref,self.vendor,'eval:test',DockerLimits());evaluate.assert_not_called()
  self.assertEqual(s['metrics']['reconstruction_iou']['status'],'blocked')
  save_summary_and_report(self.root,s)
  self.assertIn('未评分',(self.root/'report.html').read_text(encoding='utf-8'))
 def test_existing_run_preserves_original_summary(self):
  original=b'{"original":"unchanged"}';(self.root/'summary.json').write_bytes(original)
  s=complete_evaluation(self.summary(),self.root,self.ref,None,'eval:test',DockerLimits())
  save_summary_and_report(self.root,s,existing_run=True)
  self.assertEqual((self.root/'summary.json').read_bytes(),original)
  self.assertTrue((self.root/'evaluation-report.html').exists())
  with self.assertRaises(FileExistsError):save_summary_and_report(self.root,s,existing_run=True)
 def test_evaluation_config_requires_reference_without_requesting_api(self):
  with self.assertRaises(ValueError):configure(parser().parse_args(['evaluate','--run-dir',str(self.root),'--iou-vendor',str(self.vendor)]))
  args=configure(parser().parse_args(['evaluate','--run-dir',str(self.root),'--iou-vendor',str(self.vendor),'--reference-step',str(self.ref)]))
  self.assertEqual(args.command,'evaluate')
 def test_automatic_closeout_in_cli_run(self):
  from benchcad_cadquery.cli import run
  runroot=self.root/'fullrun'
  args=configure(parser().parse_args(['run','--run-dir',str(runroot),'--step',str(self.ref),'--model','chosen','--reconstruct','--iou-vendor',str(self.vendor)]))
  stage={'submitted':True,'ended_by':'submitted','decision_rounds':1,'code_executions':1,'wall_seconds':1,'bundle':str(self.bundle),'local_usage_records':[],'usage_records':[]}
  with patch('benchcad_cadquery.cli._load_encoding'),patch('benchcad_cadquery.cli.OpenAIResponses'),patch('benchcad_cadquery.cli.stage_episode',return_value=stage),patch('subprocess.run') as command,patch('evaluation.pipeline.evaluate_frozen',return_value=self.result('candidate',.3)),patch('builtins.print'):
   command.return_value.returncode=0
   self.assertEqual(run(args),0)
  result=json.loads((runroot/'summary.json').read_text());self.assertEqual(result['metrics']['reconstruction_iou']['status'],'computed');self.assertTrue((runroot/'report.html').exists())

if __name__=='__main__':unittest.main()
