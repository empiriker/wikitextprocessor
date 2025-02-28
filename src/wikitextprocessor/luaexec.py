# Helper functions for interfacing with the Lua sandbox for executing Lua
# macros in Wikitext (Wiktionary, Wikipedia, etc.)
#
# Copyright (c) Tatu Ylonen.  See file LICENSE and https://ylonen.org

import copy
import functools
import html
import json
import logging
import multiprocessing  # XXX debug, remove me
import re
import sys
import traceback
import unicodedata
from collections import deque
from functools import partial

if sys.version_info < (3, 10):
    from importlib_resources import files
else:
    from importlib.resources import files

from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    ItemsView,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
)

import lupa.lua51 as lupa
from lupa.lua51 import lua_type

from .interwiki import mw_site_interwikiMap
from .parserfns import PARSER_FUNCTIONS, call_parser_function, tag_fn

if TYPE_CHECKING:
    from lupa.lua51 import _LuaTable

    from .core import NamespaceDataEntry, Page, ParentData, Wtp

# List of search paths for Lua libraries
BUILTIN_LUA_SEARCH_PATHS: List[Tuple[str, List[str]]] = [
    # [path, ignore_modules]
    (".", ["string", "debug"]),
    ("mediawiki-extensions-Scribunto/includes/Engines/LuaCommon/lualib", []),
]

# Determine which directory our data files are in
LUA_DIR = files("wikitextprocessor") / "lua"


def lua_loader(ctx: "Wtp", modname: str) -> Optional[str]:
    """This function is called from the Lua sandbox to load a Lua module.
    This will load it from either the user-defined modules on special
    pages or from a built-in module in the file system.  This returns None
    if the module could not be loaded."""
    # print("LUA_LOADER IN PYTHON:", modname)
    assert isinstance(modname, str)
    modname = modname.strip()
    ns_data = ctx.NAMESPACE_DATA["Module"]
    ns_prefix = ns_data["name"] + ":"
    ns_alias_prefixes = tuple(alias + ":" for alias in ns_data["aliases"])
    if modname.startswith(ns_alias_prefixes):
        modname = ns_prefix + modname[modname.find(":") + 1 :]

    # Local name usually not used in Lua code
    if modname.startswith(("Module:", ns_prefix)):
        # First try to load it as a module
        if modname.startswith(("Module:_", ns_prefix + "_")):
            # Module names starting with _ are considered internal and cannot be
            # loaded from the dump file for security reasons.  This is to ensure
            # that the sandbox always gets loaded from a local file.
            data = None
        else:
            data = ctx.get_page_body(modname, ns_data["id"])
    else:
        # Try to load it from a file
        path = modname
        path = re.sub(r"[\0-\037]", "", path)  # Remove control chars, e.g. \n
        path = path.replace(":", "/")
        path = path.replace(" ", "_")
        path = re.sub(r"//+", "/", path)  # Replace multiple slashes by one
        path = re.sub(r"\.\.+", ".", path)  # Replace .. and longer by .
        path = re.sub(r"^//+", "", path)  # Remove initial slashes
        path += ".lua"
        data = None

        for prefix, exceptions in BUILTIN_LUA_SEARCH_PATHS:
            if modname in exceptions:
                continue

            file_path = LUA_DIR / prefix / path
            if file_path.is_file():
                with file_path.open("r", encoding="utf-8") as f:
                    data = f.read()
                break

    return data


def mw_text_decode(text: str, decodeNamedEntities: bool) -> str:
    """Implements the mw.text.decode function for Lua code."""
    if decodeNamedEntities:
        return html.unescape(text)

    # Otherwise decode only selected entities
    parts: List[str] = []
    pos = 0
    for m in re.finditer(r"&(lt|gt|amp|quot|nbsp);", text):
        if pos < m.start():
            parts.append(text[pos : m.start()])
        pos = m.end()
        tag = m.group(1)
        if tag == "lt":
            parts.append("<")
        elif tag == "gt":
            parts.append(">")
        elif tag == "amp":
            parts.append("&")
        elif tag == "quot":
            parts.append('"')
        elif tag == "nbsp":
            parts.append("\xa0")
        else:
            assert False
    parts.append(text[pos:])
    return "".join(parts)


def mw_text_encode(text: str, charset: str) -> str:
    """Implements the mw.text.encode function for Lua code."""
    parts: List[str] = []
    for ch in str(text):
        if ch in charset:
            chn = ord(ch)
            if chn in html.entities.codepoint2name:
                parts.append("&" + html.entities.codepoint2name[chn] + ";")
            else:
                parts.append(ch)
        else:
            parts.append(ch)
    return "".join(parts)


def mw_text_jsondecode(ctx: "Wtp", s: str, *rest: int) -> Dict[Any, Any]:
    flags = int(rest[0]) if rest else 0
    value: Dict = json.loads(s)
    assert isinstance(ctx.lua, lupa.LuaRuntime)
    # Assign locally to assure type-checker this exists
    table_from = ctx.lua.table_from

    def recurse(x: Union[List, Tuple, Dict]) -> Any:
        if isinstance(x, (list, tuple)):
            return table_from(list(map(recurse, x)))
        if not isinstance(x, dict):
            return x
        # It is a dict.
        if (flags & 1) == 1:
            # JSON_PRESERVE_KEYS flag means we don't convert keys.
            return table_from({k: recurse(v) for k, v in x.items()})
        # Convert numeric keys to integers and see if we can make it a
        # table with sequential integer keys.
        for k, v in list(x.items()):
            if k.isdigit():
                del x[k]
                x[int(k)] = recurse(v)
            else:
                x[k] = recurse(v)
        if not all(isinstance(k, int) for k in x.keys()):
            return table_from(x)
        keys = list(sorted(x.keys()))
        if not all(keys[i] == i + 1 for i in range(len(keys))):
            return table_from(x)
        # Old unused print value? XXX remove this if you can't figure out
        # what it's for.
        # values = list(x[i + 1] for i in range(len(keys)))
        return table_from(x)

    value = recurse(value)
    return value


def mw_text_jsonencode(s: Any, *rest) -> str:
    flags = int(rest[0]) if rest else 0

    def recurse(x) -> Any:
        if isinstance(x, (str, int, float, type(None), type(True))):
            return x
        if lupa.lua_type(x) == "table":
            conv_to_dict = (flags & 1) != 0  # JSON_PRESERVE_KEYS flag
            if not conv_to_dict:
                # Also convert to dict if keys are not sequential integers
                # starting from 1
                if not all(isinstance(k, int) for k in x.keys()):
                    conv_to_dict = True
                else:
                    keys = list(sorted(x.keys()))
                    if not all(keys[i] == i + 1 for i in range(len(keys))):
                        conv_to_dict = True
            if conv_to_dict:
                ht = {}
                for k, v in x.items():
                    ht[str(k)] = recurse(v)
                return ht
            # Convert to list (JSON array)
            return list(map(recurse, x.values()))
        return x

    value = recurse(s)
    return json.dumps(value, sort_keys=True)


def get_page_info(ctx: "Wtp", title: str, namespace_id: int) -> "_LuaTable":
    """Retrieves information about a page identified by its table (with
    namespace prefix.  This returns a lua table with fields "id", "exists",
    and "redirectTo".  This is used for retrieving information about page
    titles."""
    assert ctx.lua is not None

    page_id = 0  # XXX collect required info in phase 1
    page = ctx.get_page(title, namespace_id)
    # whether the page exists and what its id might be
    dt = {
        "id": page_id,
        "exists": page is not None,
        "redirectTo": page.redirect_to if page is not None else None,
    }
    return ctx.lua.table_from(dt)


def fetch_language_name(code: str, inLanguage: Optional[str]) -> str:
    """
    This function is called from Lua code as part of the mw.language
    implementation.  This maps a language code to its name.
    https://www.mediawiki.org/wiki/Extension:Scribunto/Lua_reference_manual#mw.language.fetchLanguageName
    """
    from mediawiki_langcodes import code_to_name

    return code_to_name(code, inLanguage or "")


def fetch_language_names(
    ctx: "Wtp", inLanguage: Optional[str], include: Optional[str]
) -> "_LuaTable":
    """This function is called from Lua code as part of the mw.language
    implementation.  This returns a list of known language names."""
    from mediawiki_langcodes import get_all_names

    names = {
        lang_code: lang_name
        for lang_code, lang_name in get_all_names(inLanguage or "")
    }
    return ctx.lua.table_from(names)  # type: ignore[union-attr]
    # ⇑⇑ tells mypy to ignore an 'error';
    # if fetch_language_names is being called,
    # ctx.lua.table_from should never be None.


def call_set_functions(
    ctx: "Wtp", set_functions: Callable[["_LuaTable"], None]
) -> None:
    def debug_mw_text_jsondecode(x: str, *rest: int) -> Dict[Any, Any]:
        return mw_text_jsondecode(ctx, x, *rest)

    def debug_get_page_info(title: str, ns_id: int, *bad_args) -> "_LuaTable":
        """Debug wrapper; *bad_args are a debugging parameter list that should
        not be populated, ever, but does; somewhere, the references to
        these functions in particular are being scrambled and the functions
        get parameters meant for other functions!"""
        if bad_args:  # Somehow, the functions have been scrambled in memory
            # and this function is being called with too many
            # argument
            print(
                f"MAKE_FRAME GET_PAGE_INFO DEBUG:"
                f" {repr(bad_args)},"
                f" {ctx.title=},"
                f" {multiprocessing.current_process().name}"
            )
        return get_page_info(ctx, title, ns_id)

    def debug_get_page_content(
        title: str, namespace_id: int, *bad_args: Any
    ) -> Optional[str]:
        if bad_args:
            print(
                f"MAKE_FRAME GET_PAGE_CONTENT DEBUG:"
                f" {repr(bad_args)},"
                f" {ctx.title=},"
                f" {multiprocessing.current_process().name}"
            )
        return ctx.get_page_body(title, namespace_id)

    def debug_fetch_language_name(
        code: str, inLanguage: str, *bad_args: Any
    ) -> str:
        if bad_args:
            print(
                f"MAKE_FRAME FETCH_LANGUAGE_NAME DEBUG:"
                f" {repr(bad_args)},"
                f" {ctx.title=},"
                f" {multiprocessing.current_process().name}"
            )
        return fetch_language_name(code, inLanguage)

    def debug_fetch_language_names(
        inLanguage: str, include: str, *bad_args: Any
    ) -> "_LuaTable":
        if bad_args:
            print(
                f"MAKE_FRAME FETCH_LANGUAGE_NAMES DEBUG:"
                f" {repr(bad_args)},"
                f" {ctx.title=},"
                f" {multiprocessing.current_process().name}"
            )
        return fetch_language_names(ctx, inLanguage, include)

    # Set functions that are implemented in Python
    set_functions(
        ctx.lua.table_from(
            {
                "mw_decode_python": mw_text_decode,
                "mw_encode_python": mw_text_encode,
                "mw_jsonencode_python": mw_text_jsonencode,
                "mw_jsondecode_python": debug_mw_text_jsondecode,
                "mw_python_get_page_info": debug_get_page_info,
                "mw_python_get_page_content": debug_get_page_content,
                "mw_python_fetch_language_name": debug_fetch_language_name,
                "mw_python_fetch_language_names": debug_fetch_language_names,
                "mw_wikibase_getlabel_python": partial(
                    mw_wikibase_getlabel, lang_code=ctx.lang_code
                ),
                "mw_wikibase_getdesc_python": partial(
                    mw_wikibase_getdescription, lang_code=ctx.lang_code
                ),
                "mw_current_title_python": partial(get_current_title, ctx),
                "current_frame_python": partial(
                    top_lua_stack, ctx.lua_frame_stack
                ),
                "mw_site_interwikiMap_py": partial(mw_site_interwikiMap, ctx),
            }
        )
    )


def set_global_lua_variable(lua, var_name, var_value):
    set_var_func = lua.eval(f"function(py_value) {var_name} = py_value end")
    set_var_func(var_value)


def set_lua_env_funcs(lua, wtp):
    set_global_lua_variable(
        lua, "_python_append_env", partial(append_lua_stack, wtp.lua_env_stack)
    )
    set_global_lua_variable(
        lua, "_python_top_env", partial(top_lua_stack, wtp.lua_env_stack)
    )


def initialize_lua(ctx: "Wtp") -> None:
    def filter_attribute_access(
        obj: Any, attr_name: str, is_setting: bool
    ) -> str:
        if isinstance(attr_name, str) and not attr_name.startswith("_"):
            return attr_name
        raise AttributeError("access denied")

    lua = lupa.LuaRuntime(
        unpack_returned_tuples=True,
        register_eval=False,
        attribute_filter=filter_attribute_access,
    )
    ctx.lua = lua
    lua_namespace_data = copy.deepcopy(ctx.NAMESPACE_DATA)
    ns_name: str
    ns_data: NamespaceDataEntry
    for ns_name, ns_data in lua_namespace_data.items():
        for k, v in ns_data.items():
            if isinstance(v, list):
                lua_namespace_data[ns_name][k] = lua.table_from(v)  # type: ignore[literal-required]
        lua_namespace_data[ns_name] = lua.table_from(  # type: ignore[assignment]
            lua_namespace_data[ns_name]
        )
    set_global_lua_variable(
        lua, "NAMESPACE_DATA", lua.table_from(lua_namespace_data)
    )
    set_lua_env_funcs(lua, ctx)

    # Load Lua sandbox Phase 1.  This is a very minimal file that only sets
    # the Lua loader to our custom loader; we will then use it to load the
    # bigger phase 2 of the sandbox.  This way, most of the sandbox loading
    # will benefit from caching and precompilation (when implemented).
    with (LUA_DIR / "_sandbox_phase1.lua").open(encoding="utf-8") as f:
        phase1_result: "_LuaTable" = lua.execute(f.read())
        set_loader = phase1_result[1]
        clear_loaddata_cache = phase1_result[2]
        # Call the function that sets the Lua loader
        set_loader(partial(lua_loader, ctx))

    # Then load the second phase of the sandbox.  This now goes through the
    # new loader and is evaluated in the sandbox.  This mostly implements
    # compatibility code.
    ret: "_LuaTable" = lua.eval('new_require("_sandbox_phase2")')
    # Lua tables start indexing from 1
    set_functions = ret[1]
    ctx.lua_invoke = ret[2]
    ctx.lua_reset_env = ret[3]
    ctx.lua_clear_loaddata_cache = clear_loaddata_cache

    # Set Python functions for Lua
    call_set_functions(ctx, set_functions)


def call_lua_sandbox(
    ctx: "Wtp",
    invoke_args: Iterable,
    expander: Callable,
    parent: Optional["ParentData"],
    timeout: Union[None, float, int],
) -> str:
    """Calls a function in a Lua module in the Lua sandbox.
    ``invoke_args`` is the arguments to the call; ``expander`` should
    be a function to expand an argument.  ``parent`` should be None or
    (parent_title, parent_args) for the parent page."""
    assert isinstance(invoke_args, (list, tuple))
    assert callable(expander)
    assert parent is None or isinstance(parent, tuple)
    assert timeout is None or isinstance(timeout, (int, float))

    # print("{}: CALL_LUA_SANDBOX: {} {}"
    #       .format(ctx.title, invoke_args, parent))

    if len(invoke_args) < 2:
        ctx.debug(
            "#invoke {} with too few arguments".format(invoke_args),
            sortid="luaexec/369",
        )
        return "{{" + invoke_args[0] + ":" + "|".join(invoke_args[1:]) + "}}"

    # Initialize the Lua sandbox if not already initialized
    if len(ctx.lua_env_stack) == 0:
        if ctx.lua is None:
            # This is the first call to the Lua sandbox.
            # Create a Lua context and initialize it.
            initialize_lua(ctx)  # This sets ctx.lua
        else:
            # This is a second or later call to the Lua sandbox.
            # Reset the Lua context back to initial state.
            ctx.lua_reset_env()  # type: ignore[misc]
            phase2_ret: "_LuaTable" = ctx.lua.eval(
                'new_require("_sandbox_phase2")'
            )
            # Lua tables start indexing on 1
            set_functions = phase2_ret[1]
            ctx.lua_invoke = phase2_ret[2]
            ctx.lua_reset_env = phase2_ret[3]
            call_set_functions(ctx, set_functions)

    lua = ctx.lua

    # Get module and function name
    modname = expander(invoke_args[0]).strip()
    modfn = expander(invoke_args[1]).strip()

    def make_frame(
        pframe: Union[None, Dict, "_LuaTable"],
        title: str,
        args: Union[Dict[Union[str, int], str], Tuple, List],
    ) -> "_LuaTable":
        assert isinstance(title, str)
        assert isinstance(args, (list, tuple, dict))
        if TYPE_CHECKING:
            assert lua is not None
        # Convert args to a dictionary with default value None
        if isinstance(args, dict):
            frame_args = {}
            for k, arg in args.items():
                arg = re.sub(r"(?si)<\s*noinclude\s*/\s*>", "", arg)
                frame_args[k] = arg
        else:
            assert isinstance(args, (list, tuple))
            frame_args = {}
            num = 1
            for arg in args:
                # |-separated strings in {{templates|arg=value|...}}
                m = re.match(r"""(?s)^\s*([^<>="']+?)\s*=\s*(.*?)\s*$""", arg)
                if m:
                    # Have argument name
                    k, arg = m.groups()
                    if k.isdigit():
                        k = int(k)
                        if k < 1 or k > 1000:
                            ctx.warning(
                                "Template argument index <1 "
                                f"or >1000: {k=!r}",
                                sortid="luaexec/477/20230710",
                            )
                            k = 1000
                        if num <= k:
                            num = k + 1
                else:
                    # No argument name
                    k = num
                    num += 1
                if k in frame_args:
                    ctx.warning(
                        f"Template index already in args: {k=!r}",
                        sortid="luaexec/488/20230710",
                    )
                # Remove any <noinclude/> tags; they are used to prevent
                # certain token interpretations in Wiktionary
                # (e.g., Template:cop-fay-conj-table), whereas Lua code
                # does not always like them (e.g., remove_links() in
                # Module:links).
                arg = re.sub(r"(?si)<\s*noinclude\s*/\s*>", "", arg)
                frame_args[k] = arg
        frame_args_lt: "_LuaTable" = lua.table_from(frame_args)  # type: ignore[union-attr]

        def extensionTag(frame: "_LuaTable", *args: Any) -> str:
            if len(args) < 1:
                ctx.debug(
                    "lua extensionTag with missing arguments",
                    sortid="luaexec/464",
                )
                return ""
            dt = args[0]
            if not isinstance(dt, (str, int, float, type(None))):
                name: str = str(dt["name"] or "")
                content: str = str(dt["content"] or "")
                attrs: Union[Dict[Union[int, str], str], str, "_LuaTable"] = (
                    dt["args"] or {}
                )
            elif len(args) == 1:
                name = str(args[0])
                content = ""
                attrs = {}
            elif len(args) == 2:
                name = str(args[0] or "")
                content = str(args[1] or "")
                attrs = {}
            else:
                name = str(args[0] or "")
                content = str(args[1] or "")
                attrs = args[2] or {}
            if isinstance(attrs, ItemsView) or lua_type(attrs) == "table":
                if TYPE_CHECKING:
                    # Because Lupa doesn't let us import _LuaTable directly,
                    # this work-around is needed to convince the type-checker.
                    # lua_type() is completely opaque.
                    assert isinstance(attrs, (ItemsView, _LuaTable))
                attrs2 = list(
                    v
                    if isinstance(k, int)
                    else '{}="{}"'.format(k, html.escape(v, quote=True))
                    for k, v in sorted(attrs.items(), key=lambda x: str(x[0]))
                )
            elif not attrs:
                attrs2 = []
            else:
                assert isinstance(attrs, str)
                attrs2 = [attrs]

            ctx.expand_stack.append("extensionTag()")
            ret: str = tag_fn(
                ctx, "#tag", [name, content] + attrs2, lambda x: x
            )  # Already expanded
            ctx.expand_stack.pop()
            # Expand any templates from the result
            ret = preprocess(frame, ret)
            return ret

        def callParserFunction(frame: "_LuaTable", *args: Any) -> str:
            if len(args) < 1:
                ctx.debug(
                    "lua callParserFunction missing name", sortid="luaexec/506"
                )
                return ""
            name_or_table: Union[str, "_LuaTable", Dict] = args[0]
            new_args: Union[Dict, List]
            if not isinstance(name_or_table, str):
                # name is _LuaTable
                new_args1: Union["_LuaTable", Dict, str] = name_or_table["args"]
                if isinstance(new_args1, str):
                    new_args = {1: new_args1}
                else:
                    new_args = dict(new_args1)
                name = str(name_or_table["name"]) or ""
            else:
                new_args = []
                name = name_or_table
                for arg in args[1:]:
                    if isinstance(arg, (int, float, str)):
                        new_args.append(str(arg))
                    else:
                        for k, v in sorted(
                            arg.items(), key=lambda x: str(x[0])
                        ):
                            new_args.append(str(v))
            name = ctx._canonicalize_parserfn_name(name)
            if name not in PARSER_FUNCTIONS:
                ctx.debug(
                    "lua frame callParserFunction() undefined "
                    "function {!r}".format(name),
                    sortid="luaexec/529",
                )
                return ""
            return call_parser_function(ctx, name, new_args, lambda x: x)

        def expand_all_templates(encoded: str) -> str:
            # Expand all templates here, even if otherwise only
            # expanding some of them.  We stay quiet about undefined
            # templates here, because Wiktionary Module:ugly hacks
            # generates them all the time.
            ret = ctx.expand(encoded, parent, quiet=True)
            return ret

        def preprocess(frame: "_LuaTable", *args: Any) -> str:
            if len(args) < 1:
                ctx.debug(
                    "lua preprocess missing argument", sortid="luaexec/545"
                )
                return ""
            candidate = args[0]
            if not isinstance(candidate, str):
                v = str(candidate["text"] or "")
            else:
                v = candidate
            # Expand all templates, in case the Lua code actually
            # inspects the output.
            v = ctx._encode(v)
            ctx.expand_stack.append("frame:preprocess()")
            ret = expand_all_templates(v)
            ctx.expand_stack.pop()
            return ret

        def expandTemplate(frame: "_LuaTable", *args) -> str:
            if len(args) < 1:
                ctx.debug(
                    "lua expandTemplate missing arguments", sortid="luaexec/561"
                )
                return ""
            dt = args[0]
            if isinstance(dt, (int, float, str, type(None))):
                ctx.debug(
                    "lua expandTemplate arguments should be named",
                    sortid="luaexec/566",
                )
                return ""
            if TYPE_CHECKING:
                assert isinstance(dt, (_LuaTable, Dict))
            title = dt["title"] or ""
            args2 = dt["args"] or {}
            new_args = [title]
            for k, v in sorted(args2.items(), key=lambda x: str(x[0])):
                new_args.append("{}={}".format(k, v))
            encoded = ctx._save_value("T", new_args, False)
            ctx.expand_stack.append("frame:expandTemplate()")
            ret = expand_all_templates(encoded)
            ctx.expand_stack.pop()
            return ret

        def debugGetParent(frame: "_LuaTable", *args) -> "_LuaTable":
            if args:
                ctx.debug(
                    f"LAMBDA GETPARENT EXTRA ARGS: Lua module:{title}, "
                    f"frame: {frame}, "
                    f"extra args: {repr(args)}, "
                    f"process name: {multiprocessing.current_process().name}"
                )
            if TYPE_CHECKING:
                assert isinstance(pframe, _LuaTable)
            return pframe

        # XXX this is actually pretty generic and could be used
        # to wrap any python function in a lua function wrapper if
        # needed; could this be turned into a *decorator*?
        # debugGetParent needed to be wrapped because there's a
        # en.wikiPEDIA module that tested type(x.getParent) == 'function'
        # for some silly reason; if that turns up elsewhere, here's
        # a solution.
        lua_wrapper_generator = lua.eval(
            """
            function(py_func)
                wrapper_func = function(...)
                    return py_func(...)
                end
                return wrapper_func
            end
        """
        )

        wrappedDebugGetParent = lua_wrapper_generator(debugGetParent)

        def debugGetTitle(frame: "_LuaTable", *args) -> str:
            if args:
                ctx.debug(
                    f"LAMBDA GETTITLE EXTRA ARGS: Lua module:{title}, "
                    f"frame: {frame}, "
                    f"extra args: {repr(args)}, "
                    f"process name: {multiprocessing.current_process().name}"
                )
            return title

        # Create frame object as dictionary with default value None
        frame: Dict[str, Union["_LuaTable", Callable]] = {}
        frame["args"] = frame_args_lt
        frame["callParserFunction"] = callParserFunction
        frame["extensionTag"] = extensionTag
        frame["expandTemplate"] = expandTemplate
        frame["getParent"] = wrappedDebugGetParent
        frame["getTitle"] = debugGetTitle
        frame["preprocess"] = preprocess
        # argumentPairs, getArgument, newChild, newParserValue,
        # newTemplateParserValue are set in _sandbox_phase2.lua
        return lua.table_from(frame)

    # Create parent frame (for page being processed) and current frame
    # (for module being called)
    if parent is not None:
        parent_title: str
        page_args: Union["_LuaTable", Dict]
        parent_title, page_args = parent
        expanded_key_args = {}
        for k, v in page_args.items():
            if isinstance(k, str):
                expanded_key_args[expander(k)] = v
            else:
                expanded_key_args[k] = v
        pframe = make_frame(None, parent_title, expanded_key_args)
    else:
        pframe = None
    frame = make_frame(pframe, modname, invoke_args[2:])

    # Call the Lua function in the given module
    stack_len = len(ctx.expand_stack)
    ctx.expand_stack.append("Lua:{}:{}()".format(modname, modfn))
    if TYPE_CHECKING:
        assert ctx.lua_invoke is not None
    lua_exception: Optional[Exception] = None
    try:
        ctx.lua_frame_stack.append(frame)
        ret: Tuple[bool, str] = ctx.lua_invoke(
            modname, modfn, frame, ctx.title, timeout
        )
        # Lua functions returning multiple values will return a tuple
        # as would be normal in Python.
        if not isinstance(ret, (list, tuple)):
            ok, text = ret, ""
        elif len(ret) == 1:
            ok, text = ret[0], ""
        else:
            ok, text = ret[0], ret[1]
    except UnicodeDecodeError:
        ctx.debug(
            "invalid unicode returned from lua by {}: parent {}".format(
                invoke_args, parent
            ),
            sortid="luaexec/626",
        )
        ok, text = True, ""
    except lupa.LuaError as e:
        ok, text, lua_exception = False, "", e
    finally:
        while len(ctx.expand_stack) > stack_len:
            ctx.expand_stack.pop()
    # print("Lua call {} returned: ok={!r} text={!r}"
    #       .format(invoke_args, ok, text))
    if len(ctx.lua_env_stack) > 0:
        ctx.lua_env_stack.pop()
    if len(ctx.lua_frame_stack) > 0:
        ctx.lua_frame_stack.pop()
    if ok:  # XXX should this be "is True" instead of checking truthiness?
        text = str(text) if text is not None else ""
        text = unicodedata.normalize("NFC", text)
        return text
    if lua_exception is not None:
        text = "".join(traceback.format_exception(lua_exception)).strip()
    elif not isinstance(text, str):
        text = str(text)
    msg = re.sub(r".*?:\d+: ", "", text.split("\n", 1)[0])
    if "'debug.error'" in text:
        if not msg.startswith("This template is deprecated."):
            ctx.debug("lua error -- " + msg, sortid="luaexec/659")
    elif "Translations must be for attested and approved " in text:
        # Ignore this error - it is an error but a clear error in Wiktionary
        # rather than in the extractor.
        return ""
    elif (
        "attempt to index a nil value (local 'lang')" in text
        and "in function 'Module:links.getLinkPage'" in text
    ):
        # Ignore this error - happens when an unknown language code is passed
        # to various templates (a Wiktionary error, not extractor error)
        return ""
    else:
        if "check deprecated lang param usage" in ctx.expand_stack:
            ctx.debug(
                "LUA error but likely not bug"
                "-- in #invoke {} parent {}".format(invoke_args, parent),
                trace=text,
                sortid="luaexec/679",
            )
        else:
            ctx.error(
                "LUA error in #invoke"
                "{} parent {}".format(invoke_args, parent),
                trace=text,
                sortid="luaexec/683",
            )
    msg = "Lua execution error"
    if "Lua timeout error" in text:
        msg = "Lua timeout error"
    return (
        '<strong class="error">{} in Module:{} function {}'
        "</strong>".format(msg, html.escape(modname), html.escape(modfn))
    )


@functools.cache
def query_wikidata(item_id: str, lang_code: str) -> Optional[dict]:
    import requests

    r = requests.get(
        "https://query.wikidata.org/sparql",
        params={
            "query": f"""
            SELECT ?itemLabel ?itemDescription WHERE {{
              VALUES ?item {{ wd:{item_id} }}.
              SERVICE wikibase:label {{
                bd:serviceParam
                wikibase:language "{lang_code},[AUTO_LANGUAGE],en".
              }}
            }}
            """,
            "format": "json",
        },
        headers={"user-agent": "wikitextprocessor"},
    )

    if r.ok:
        result = r.json()
        # print(f"WIKIDATA QUERY succeeded: {item_id=}, {result=}")
        for binding in result.get("results", {}).get("bindings", {}):
            return binding
    else:
        logging.error(f"WIKIDATA QUERY failed: {item_id=} {r.text=}")
        return None
    return None


def mw_wikibase_getlabel(item_id: str, lang_code: str = "en") -> Optional[str]:
    item_data = query_wikidata(item_id, lang_code)
    if item_data is not None:
        return item_data.get("itemLabel", {}).get("value", item_id)
    else:
        return None


def mw_wikibase_getdescription(
    item_id: str, lang_code: str = "en"
) -> Optional[str]:
    item_data = query_wikidata(item_id, lang_code)
    if item_data is not None:
        return item_data.get("itemDescription", {}).get("value", item_id)
    else:
        return None


def top_lua_stack(env_stack: deque) -> Optional["_LuaTable"]:
    if len(env_stack) == 0:
        return None
    else:
        return env_stack[-1]


def append_lua_stack(env_stack: deque, env: "_LuaTable") -> None:
    env_stack.append(env)


def get_current_title(wtp: "Wtp") -> str:
    return wtp.title
