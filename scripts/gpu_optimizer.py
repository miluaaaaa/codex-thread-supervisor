"""Per-task cadence from owner observations; never GPU admission or model proof."""
import math
import time


def validate_metrics(value, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise ValueError('gpu metrics must be an object')
    result = {}
    for key in ('observed_at', 'target_gpus', 'verified_gpus', 'ready_lanes'):
        n = value.get(key)
        if type(n) is not int or n < 0 or (key != 'observed_at' and n > 4096):
            raise ValueError('invalid gpu metric: ' + key)
        result[key] = n
    if result['target_gpus'] < 1 or not 0 <= now - result['observed_at'] <= 600:
        raise ValueError('invalid target or stale/future GPU observation')
    capacity = value.get('capacity', 'unknown')
    if capacity not in ('available', 'unavailable', 'unknown'):
        raise ValueError('invalid capacity')
    result['capacity'] = capacity
    evidence = value.get('evidence', '')
    if not isinstance(evidence, str) or len(evidence) > 1024 or (result['verified_gpus'] and not evidence.strip()):
        raise ValueError('verified GPU count requires execution evidence reference')
    result['evidence'] = evidence
    for key in ('job_runtime_seconds', 'next_completion_seconds'):
        if key in value:
            n = value[key]
            if type(n) not in (int, float) or not math.isfinite(n) or n < 0:
                raise ValueError('invalid gpu metric: ' + key)
            result[key] = n
    return result


def observe(state, metrics, now=None):
    metrics = validate_metrics(metrics, now)
    previous = state.get('gpu_metrics')
    if previous and metrics['observed_at'] <= previous['observed_at']:
        if metrics == previous:
            return  # Duplicate delivery must not accumulate statistics.
        raise ValueError('GPU observations must be strictly ordered')
    stats = state.setdefault('gpu_task_stats', {'observations': 0, 'observed_seconds': 0,
        'unknown_seconds': 0, 'target_gpu_seconds': 0, 'reported_gpu_seconds': 0,
        'shortfall_gpu_seconds': 0})
    if previous:
        elapsed = metrics['observed_at'] - previous['observed_at']
        if elapsed <= 600:
            target = previous['target_gpus']
            running = min(target, previous['verified_gpus'])
            stats['observed_seconds'] += elapsed
            stats['target_gpu_seconds'] += elapsed * target
            stats['reported_gpu_seconds'] += elapsed * running
            stats['shortfall_gpu_seconds'] += elapsed * (target - running)
        else:
            stats['unknown_seconds'] += elapsed
    stats['observations'] += 1
    state['gpu_metrics'] = metrics


def choose_interval(state, now=None):
    now = time.time() if now is None else now
    base = int(state.get('base_interval_seconds', state.get('interval_seconds', 300)))
    config = state.get('gpu_optimizer', {})
    minimum, maximum = config.get('min_seconds', 60), config.get('max_seconds', 900)
    m = state.get('gpu_metrics')
    if not m or not 0 <= now - m['observed_at'] <= 600:
        return minimum, 'needs_fresh_task_metrics'
    if m['ready_lanes'] and m['verified_gpus'] < m['target_gpus']:
        if m['capacity'] == 'unavailable':
            return min(maximum, max(minimum, 180)), 'waiting_capacity'
        return minimum, 'runnable_task_shortfall'
    if m['verified_gpus'] == 0:
        return min(maximum, max(minimum, base)), 'no_runnable_work'
    # Expected polling delay is interval/2. Budget it to 5% of job runtime.
    desired = base
    runtime = m.get('job_runtime_seconds')
    if runtime:
        desired = round(2 * config.get('idle_fraction', .05) * runtime)
    eta = m.get('next_completion_seconds')
    if eta is not None:
        # ETA is relative to the observation, not to every subsequent timer tick.
        remaining = max(0, eta - (now - m['observed_at']))
        desired = min(desired, round(remaining / 4))
    return min(maximum, max(minimum, desired)), 'running_completion_horizon'


def apply_optimizer(state, now=None):
    if state.get('gpu_optimizer', {}).get('enabled'):
        interval, reason = choose_interval(state, now)
        state['interval_seconds'] = interval
        state['gpu_optimizer']['reason'] = reason


def prompt_direction(state):
    if not state.get('gpu_optimizer', {}).get('enabled'):
        return ''
    return ('按任务维持用户指定GPU数量，准备有效工作并及时补位；不要等待监督才派发。'
            '在状态JSON中附gpu对象：observed_at为本轮检查Unix秒，target_gpus为用户目标，'
            'verified_gpus只计同模型PID显存及非零计算已核验的卡，ready_lanes为已预检待跑lane，'
            'capacity为available/unavailable/unknown，evidence为执行证据引用；'
            '可附job_runtime_seconds及next_completion_seconds，未知不猜。'
            '作业完成/失败时可通过observe --metrics-file更新本线程统计和周期，'
            '仍走Zen准入、独占卡、严格结果校验；分配数不等于执行数。')
