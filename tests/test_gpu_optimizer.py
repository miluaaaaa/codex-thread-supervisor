import time
import unittest
from gpu_optimizer import validate_metrics, observe, choose_interval


class OptimizerTests(unittest.TestCase):
    def metrics(self, **kwargs):
        return dict(observed_at=1000, target_gpus=8, verified_gpus=8,
                    ready_lanes=0, capacity='unknown', evidence='PID+memory+SM trace', **kwargs)

    def state(self, metrics):
        return {'base_interval_seconds': 300, 'gpu_optimizer': {'enabled': True}, 'gpu_metrics': metrics}

    def test_short_jobs_and_approaching_completion_choose_short_period(self):
        self.assertEqual(choose_interval(self.state(self.metrics(job_runtime_seconds=120)),1000)[0],60)
        m=self.metrics(job_runtime_seconds=7200,next_completion_seconds=400)
        self.assertEqual(choose_interval(self.state(m),1200)[0],60)

    def test_long_stable_work_is_bounded(self):
        self.assertEqual(choose_interval(self.state(self.metrics(job_runtime_seconds=3600)),1000)[0],360)
        self.assertEqual(choose_interval(self.state(self.metrics(job_runtime_seconds=86400)),1000)[0],900)

    def test_capacity_wait_is_bounded_and_does_not_compound(self):
        m=self.metrics();m.update(verified_gpus=2,ready_lanes=6,capacity='unavailable')
        s=self.state(m)
        for _ in range(5):
            s['interval_seconds']=choose_interval(s,1000)[0]
            self.assertEqual(s['interval_seconds'],180)
        m['capacity']='available'
        self.assertEqual(choose_interval(s,1000)[0],60)

    def test_missing_and_stale_metrics_cannot_prolong_wait(self):
        s=self.state(self.metrics())
        self.assertEqual(choose_interval(s,1601), (60,'needs_fresh_task_metrics'))
        s.pop('gpu_metrics');self.assertEqual(choose_interval(s,1000)[0],60)

    def test_bad_types_nonfinite_future_and_no_execution_reference_fail(self):
        for key,value in [('verified_gpus',True),('observed_at',1001),('evidence',''),('job_runtime_seconds',float('nan'))]:
            m=self.metrics();m[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validate_metrics(m,1000)

    def test_duplicate_and_unknown_time_do_not_invent_utilization(self):
        s={};m=self.metrics();m['verified_gpus']=6
        observe(s,m,1000);observe(s,m,1000)
        self.assertEqual(s['gpu_task_stats']['observations'],1)
        m2=dict(m,observed_at=1120);observe(s,m2,1120)
        self.assertEqual(s['gpu_task_stats']['shortfall_gpu_seconds'],240)
        self.assertEqual(s['gpu_task_stats']['reported_gpu_seconds'],720)
        observe(s,dict(m2,observed_at=2000),2000)
        self.assertEqual(s['gpu_task_stats']['unknown_seconds'],880)
        with self.assertRaises(ValueError):observe(s,m,2000)

    def test_unbounded_target_tracks_runnable_demand_without_inventing_cap(self):
        m=self.metrics();m.update(target_gpus=None,verified_gpus=11,ready_lanes=25,capacity='unavailable')
        s={};observe(s,m,1000)
        self.assertIsNone(s['gpu_metrics']['target_gpus'])
        self.assertEqual(choose_interval(s,1000)[0],180)
        m=dict(m,observed_at=1060,capacity='available');observe(s,m,1060)
        self.assertEqual(choose_interval(s,1060)[0],60)
        self.assertEqual(s['gpu_task_stats']['shortfall_gpu_seconds'],25*60)
        self.assertEqual(s['gpu_task_stats']['target_gpu_seconds'],36*60)
        m=dict(m,observed_at=1120,ready_lanes=0);observe(s,m,1120)
        self.assertEqual(choose_interval(s,1120)[0],300)
        m.pop('target_gpus')
        with self.assertRaises(ValueError):validate_metrics(m,1120)
