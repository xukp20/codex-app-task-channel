"""Explicit session configuration and private, secret-free thread bindings."""
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

SETTINGS = {
    'model', 'model_provider', 'model_reasoning_effort', 'service_tier',
    'model_context_window', 'model_auto_compact_token_limit', 'developer_instructions',
    'model_providers',
}
OPTIONS = ('model', 'effort', 'service_tier', 'context_window', 'auto_compact_token_limit', 'provider')
CONFIG_KEYS = {'effort': 'model_reasoning_effort', 'context_window': 'model_context_window',
               'auto_compact_token_limit': 'model_auto_compact_token_limit', 'provider': 'model_provider'}


def home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))


def binding_path(thread):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', thread):
        raise ValueError('invalid thread id')
    return home() / 'task-channel' / 'threads' / (thread + '.json')


def read_binding(thread):
    path = binding_path(thread)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get('schema') != 1:
        raise ValueError('unsupported task-channel binding schema')
    return data


def merge(left, right):
    result = dict(left)
    for key, value in right.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def prepare(args):
    """Read references fresh; never persist resolved credentials or instruction content."""
    binding = read_binding(args.thread) if args.command == 'resume' else None
    if binding:
        if binding['socket'] != str(args.socket):
            raise ValueError('binding belongs to another app-server socket')
        replacing = bool(args.agent_config or args.config_file)
        if not replacing:
            args.agent_config = binding.get('agent_config')
            args.config_file = binding.get('config_file')
            for key, value in binding.get('overrides', {}).items():
                if getattr(args, key, None) is None:
                    setattr(args, key, value)
    explicit = {key: getattr(args, key, None) for key in OPTIONS if getattr(args, key, None) is not None}
    config = {}
    if args.agent_config:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.agent_config):
            raise ValueError('--agent-config must be a registered role id')
        global_config = tomllib.loads((home() / 'config.toml').read_text())
        role = global_config.get('agents', {}).get(args.agent_config)
        if not role or not role.get('config_file'):
            raise ValueError('agent config is not registered')
        role_config = tomllib.loads((home() / role['config_file']).read_text())
        if role_config.get('name') != args.agent_config:
            raise ValueError('registered role name mismatch')
        config = {key: value for key, value in role_config.items() if key in SETTINGS}
    if args.config_file:
        args.config_file = str(Path(args.config_file).expanduser().resolve())
        supplied = tomllib.loads(Path(args.config_file).read_text())
        if set(supplied) - SETTINGS:
            raise ValueError('config file contains unsupported settings; permission/tool overrides are not imported')
        config = merge(config, supplied)
    for key, value in explicit.items():
        config[CONFIG_KEYS.get(key, key)] = value
    provider = config.get('model_provider')
    if provider:
        global_config = tomllib.loads((home() / 'config.toml').read_text())
        definition = merge(global_config.get('model_providers', {}).get(provider, {}),
                           config.get('model_providers', {}).get(provider, {}))
        if not definition and provider != 'openai':
            raise ValueError('selected provider has no local definition')
        if definition:
            config['model_providers'] = {provider: definition}
    elif config.get('model_providers'):
        raise ValueError('model_providers requires an explicit model_provider')
    for key in OPTIONS:
        if CONFIG_KEYS.get(key, key) in config:
            setattr(args, key, config[CONFIG_KEYS.get(key, key)])
    for key in ('model_context_window', 'model_auto_compact_token_limit'):
        if key in config and (type(config[key]) is not int or config[key] <= 0):
            raise ValueError('context settings must be positive integers')
    args.session_overrides = config
    args.binding_spec = ({'schema': 1, 'socket': str(args.socket), 'agent_config': args.agent_config,
                          'config_file': args.config_file, 'overrides': explicit}
                         if config or binding else None)


def save_binding(args, thread):
    data = getattr(args, 'binding_spec', None)
    if data is None:
        return None
    path = binding_path(thread)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.binding-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(data, threadId=thread), stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)
