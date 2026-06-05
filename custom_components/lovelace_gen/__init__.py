import os
import logging
import json
import io
import time
from collections import OrderedDict

import jinja2

from annotatedyaml import loader
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

def fromjson(value):
    return json.loads(value)

jinja = jinja2.Environment(loader=jinja2.FileSystemLoader("/"))

jinja.filters['fromjson'] = fromjson

llgen_config = {}

def load_yaml(fname, secrets = None, args={}):
    try:
        ll_gen = False
        with open(fname, encoding="utf-8") as f:
            if f.readline().lower().startswith("# lovelace_gen"):
                ll_gen = True

        if ll_gen:
            stream = io.StringIO(jinja.get_template(fname).render({**args, "_global": llgen_config}))
            stream.name = fname
            return loader.yaml.load(stream, Loader=lambda _stream: loader.PythonSafeLoader(_stream, secrets)) or OrderedDict()
        else:
            with open(fname, encoding="utf-8") as config_file:
                return loader.yaml.load(config_file, Loader=lambda stream: loader.PythonSafeLoader(stream, secrets)) or OrderedDict()
    except loader.yaml.YAMLError as exc:
        _LOGGER.error(str(exc))
        raise HomeAssistantError(exc)
    except UnicodeDecodeError as exc:
        _LOGGER.error("Unable to read file %s: %s", fname, exc)
        raise HomeAssistantError(exc)


def _include_yaml(ldr, node):
    args = {}
    if isinstance(node.value, str):
        fn = node.value
    else:
        fn, args, *_ = ldr.construct_sequence(node)
    fname = os.path.abspath(os.path.join(os.path.dirname(ldr.name), fn))
    try:
        return loader._add_reference(load_yaml(fname, ldr.secrets, args=args), ldr, node)
    except FileNotFoundError as exc:
        _LOGGER.error("Unable to include file %s: %s", fname, exc);
        raise HomeAssistantError(exc)

def _uncache_file(ldr, node):
    path = node.value
    timestamp = str(time.time())
    if '?' in path:
        return f"{path}&{timestamp}"
    return f"{path}?{timestamp}"

# ─────────────────────────────────────────────────────────────────────
# !include_dir_* directives — route through lovelace_gen's load_yaml so
# files in the loaded dir get their `# lovelace_gen` Jinja processed.
#
# HA's stock implementations call load_yaml via `from .loader import
# load_yaml`, which value-snapshots the reference at import time. The
# monkey-patch on `loader.load_yaml` happens later (when this
# integration loads), so HA's stock dir-* constructors keep using the
# stale unpatched reference. Net effect: `!include_dir_merge_list` &
# friends bypass lovelace_gen, see raw {% Jinja %}, and log/error at
# startup. These overrides fix that by calling our load_yaml directly.
#
# Walk semantics match HA core (recursive, skip dotfile dirs, sort).
# ─────────────────────────────────────────────────────────────────────

def _find_yaml_files(directory):
    """Recursive directory walk yielding sorted *.yaml paths. Skips dotfile dirs."""
    for root, dirs, files in os.walk(directory, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for basename in sorted(files):
            if basename.endswith(".yaml") and not basename.startswith("."):
                yield os.path.join(root, basename)


def _include_dir_list(ldr, node):
    loc = os.path.join(os.path.dirname(ldr.name), node.value)
    return [load_yaml(f, ldr.secrets) for f in _find_yaml_files(loc)]


def _include_dir_named(ldr, node):
    loc = os.path.join(os.path.dirname(ldr.name), node.value)
    return {
        os.path.splitext(os.path.basename(f))[0]: load_yaml(f, ldr.secrets)
        for f in _find_yaml_files(loc)
    }


def _include_dir_merge_list(ldr, node):
    loc = os.path.join(os.path.dirname(ldr.name), node.value)
    merged = []
    for f in _find_yaml_files(loc):
        sub = load_yaml(f, ldr.secrets)
        if isinstance(sub, list):
            merged.extend(sub)
    return loader._add_reference(merged, ldr, node)


def _include_dir_merge_named(ldr, node):
    loc = os.path.join(os.path.dirname(ldr.name), node.value)
    merged = {}
    for f in _find_yaml_files(loc):
        sub = load_yaml(f, ldr.secrets)
        if isinstance(sub, dict):
            merged.update(sub)
    return merged


loader.load_yaml = load_yaml

# Register constructors on BOTH loader classes. annotatedyaml has two:
# `FastSafeLoader` (C-extension, the default fast path) and
# `PythonSafeLoader` (pure-Python fallback). HA picks one based on
# whether libyaml is available; patching only PythonSafeLoader leaves
# the fast path unpatched and the upstream `!include_dir_*` Jinja race
# unfixed. The previous `!include` registration had the same gap —
# now also covered.
for _Loader in (loader.FastSafeLoader, loader.PythonSafeLoader):
    _Loader.add_constructor("!include", _include_yaml)
    _Loader.add_constructor("!include_dir_list", _include_dir_list)
    _Loader.add_constructor("!include_dir_named", _include_dir_named)
    _Loader.add_constructor("!include_dir_merge_list", _include_dir_merge_list)
    _Loader.add_constructor("!include_dir_merge_named", _include_dir_merge_named)
    _Loader.add_constructor("!file", _uncache_file)

_LOGGER.info(
    "lovelace_gen: patched !include + !include_dir_* on FastSafeLoader + PythonSafeLoader"
)

async def async_setup(hass, config):
    llgen_config.update(config.get("lovelace_gen"))

    # Custom-components load AFTER core integrations including lovelace.
    # By the time our constructor patches are in place, lovelace may
    # have already cached a broken dashboard config from when stock
    # annotatedyaml choked on `{% Jinja %}` or sequence-form `!include`.
    # Connected clients (kiosks especially) that requested the config
    # during that race window received an error.
    #
    # Fix: invalidate lovelace's cached configs RIGHT HERE in setup,
    # while we still have control. Lovelace has finished its own setup
    # by now (we load after it), so its dashboards dict is populated.
    # Any clients connecting after this point (or already-connected
    # clients receiving our `lovelace_updated` events) get a fresh
    # config parsed with our patches.

    def _refresh():
        try:
            lovelace_data = hass.data.get("lovelace")
            if lovelace_data is None:
                return None
            dashboards = getattr(lovelace_data, "dashboards", None)
            if dashboards is None:
                return None
            invalidated = []
            for url_path, dashboard in list(dashboards.items()):
                # Best-effort cache invalidation across HA versions
                for attr in ("_cache", "_config", "_data"):
                    if hasattr(dashboard, attr):
                        try:
                            setattr(dashboard, attr, None)
                        except Exception:
                            pass
                invalidated.append(url_path)
                hass.bus.async_fire(
                    "lovelace_updated",
                    {"url_path": url_path, "mode": "yaml"},
                )
            return invalidated
        except Exception as exc:
            _LOGGER.warning("lovelace_gen: refresh failed: %s", exc)
            return None

    result = _refresh()
    if result is None:
        _LOGGER.debug("lovelace_gen: lovelace data not ready at setup; falling back to post-start refresh")
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: _refresh())
    else:
        _LOGGER.info("lovelace_gen: refreshed %d dashboard(s) after race-window patches", len(result))

    return True

# Allow redefinition of node anchors
import yaml

def compose_node(self, parent, index):
    if self.check_event(yaml.events.AliasEvent):
        event = self.get_event()
        anchor = event.anchor
        if anchor not in self.anchors:
            raise yaml.composer.ComposerError(None, None, "found undefined alias %r"
                    % anchor, event.start_mark)
        return self.anchors[anchor]
    event = self.peek_event()
    anchor = event.anchor
    self.descend_resolver(parent, index)
    if self.check_event(yaml.events.ScalarEvent):
        node = self.compose_scalar_node(anchor)
    elif self.check_event(yaml.events.SequenceStartEvent):
        node = self.compose_sequence_node(anchor)
    elif self.check_event(yaml.events.MappingStartEvent):
        node = self.compose_mapping_node(anchor)
    self.ascend_resolver()
    return node

yaml.composer.Composer.compose_node = compose_node
