from collections import deque
from collections import namedtuple
import itertools
import operator

from . import operators
from .visitors import ExtendedInternalTraversal
from .visitors import InternalTraversal
from .. import util
from ..inspection import inspect
from ..util import collections_abc
from ..util import HasMemoized
from ..util import py37

SKIP_TRAVERSE = util.symbol("skip_traverse")
COMPARE_FAILED = False
COMPARE_SUCCEEDED = True
NO_CACHE = util.symbol("no_cache")
CACHE_IN_PLACE = util.symbol("cache_in_place")
CALL_GEN_CACHE_KEY = util.symbol("call_gen_cache_key")
STATIC_CACHE_KEY = util.symbol("static_cache_key")
PROPAGATE_ATTRS = util.symbol("propagate_attrs")
ANON_NAME = util.symbol("anon_name")


def compare(obj1, obj2, **kw):
    if kw.get("use_proxies", False):
        strategy = ColIdentityComparatorStrategy()
    else:
        strategy = TraversalComparatorStrategy()

    return strategy.compare(obj1, obj2, **kw)


def _preconfigure_traversals(target_hierarchy):
    for cls in util.walk_subclasses(target_hierarchy):
        if hasattr(cls, "_traverse_internals"):
            cls._generate_cache_attrs()
            _copy_internals.generate_dispatch(
                cls,
                cls._traverse_internals,
                "_generated_copy_internals_traversal",
            )
            _get_children.generate_dispatch(
                cls,
                cls._traverse_internals,
                "_generated_get_children_traversal",
            )


class HasCacheKey(object):
    _cache_key_traversal = NO_CACHE
    __slots__ = ()

    @classmethod
    def _generate_cache_attrs(cls):
        """generate cache key dispatcher for a new class.

        This sets the _generated_cache_key_traversal attribute once called
        so should only be called once per class.

        """
        inherit = cls.__dict__.get("inherit_cache", False)

        if inherit:
            _cache_key_traversal = getattr(cls, "_cache_key_traversal", None)
            if _cache_key_traversal is None:
                try:
                    _cache_key_traversal = cls._traverse_internals
                except AttributeError:
                    cls._generated_cache_key_traversal = NO_CACHE
                    return NO_CACHE

            # TODO: wouldn't we instead get this from our superclass?
            # also, our superclass may not have this yet, but in any case,
            # we'd generate for the superclass that has it.   this is a little
            # more complicated, so for the moment this is a little less
            # efficient on startup but simpler.
            return _cache_key_traversal_visitor.generate_dispatch(
                cls, _cache_key_traversal, "_generated_cache_key_traversal"
            )
        else:
            _cache_key_traversal = cls.__dict__.get(
                "_cache_key_traversal", None
            )
            if _cache_key_traversal is None:
                _cache_key_traversal = cls.__dict__.get(
                    "_traverse_internals", None
                )
                if _cache_key_traversal is None:
                    cls._generated_cache_key_traversal = NO_CACHE
                    return NO_CACHE

            return _cache_key_traversal_visitor.generate_dispatch(
                cls, _cache_key_traversal, "_generated_cache_key_traversal"
            )

    @util.preload_module("sqlalchemy.sql.elements")
    def _gen_cache_key(self, anon_map, bindparams):
        """return an optional cache key.

        The cache key is a tuple which can contain any series of
        objects that are hashable and also identifies
        this object uniquely within the presence of a larger SQL expression
        or statement, for the purposes of caching the resulting query.

        The cache key should be based on the SQL compiled structure that would
        ultimately be produced.   That is, two structures that are composed in
        exactly the same way should produce the same cache key; any difference
        in the structures that would affect the SQL string or the type handlers
        should result in a different cache key.

        If a structure cannot produce a useful cache key, the NO_CACHE
        symbol should be added to the anon_map and the method should
        return None.

        """

        idself = id(self)
        cls = self.__class__

        if idself in anon_map:
            return (anon_map[idself], cls)
        else:
            # inline of
            # id_ = anon_map[idself]
            anon_map[idself] = id_ = str(anon_map.index)
            anon_map.index += 1

        try:
            dispatcher = cls.__dict__["_generated_cache_key_traversal"]
        except KeyError:
            # most of the dispatchers are generated up front
            # in sqlalchemy/sql/__init__.py ->
            # traversals.py-> _preconfigure_traversals().
            # this block will generate any remaining dispatchers.
            dispatcher = cls._generate_cache_attrs()

        if dispatcher is NO_CACHE:
            anon_map[NO_CACHE] = True
            return None

        result = (id_, cls)

        # inline of _cache_key_traversal_visitor.run_generated_dispatch()

        for attrname, obj, meth in dispatcher(
            self, _cache_key_traversal_visitor
        ):
            if obj is not None:
                # TODO: see if C code can help here as Python lacks an
                # efficient switch construct

                if meth is STATIC_CACHE_KEY:
                    result += (attrname, obj._static_cache_key)
                elif meth is ANON_NAME:
                    elements = util.preloaded.sql_elements
                    if isinstance(obj, elements._anonymous_label):
                        obj = obj.apply_map(anon_map)
                    result += (attrname, obj)
                elif meth is CALL_GEN_CACHE_KEY:
                    result += (
                        attrname,
                        obj._gen_cache_key(anon_map, bindparams),
                    )

                # remaining cache functions are against
                # Python tuples, dicts, lists, etc. so we can skip
                # if they are empty
                elif obj:
                    if meth is CACHE_IN_PLACE:
                        result += (attrname, obj)
                    elif meth is PROPAGATE_ATTRS:
                        result += (
                            attrname,
                            obj["compile_state_plugin"],
                            obj["plugin_subject"]._gen_cache_key(
                                anon_map, bindparams
                            )
                            if obj["plugin_subject"]
                            else None,
                        )
                    elif meth is InternalTraversal.dp_annotations_key:
                        # obj is here is the _annotations dict.   however, we
                        # want to use the memoized cache key version of it. for
                        # Columns, this should be long lived.   For select()
                        # statements, not so much, but they usually won't have
                        # annotations.
                        result += self._annotations_cache_key
                    elif (
                        meth is InternalTraversal.dp_clauseelement_list
                        or meth is InternalTraversal.dp_clauseelement_tuple
                    ):
                        result += (
                            attrname,
                            tuple(
                                [
                                    elem._gen_cache_key(anon_map, bindparams)
                                    for elem in obj
                                ]
                            ),
                        )
                    else:
                        result += meth(
                            attrname, obj, self, anon_map, bindparams
                        )

        return result

    def _generate_cache_key(self):
        """return a cache key.

        The cache key is a tuple which can contain any series of
        objects that are hashable and also identifies
        this object uniquely within the presence of a larger SQL expression
        or statement, for the purposes of caching the resulting query.

        The cache key should be based on the SQL compiled structure that would
        ultimately be produced.   That is, two structures that are composed in
        exactly the same way should produce the same cache key; any difference
        in the structures that would affect the SQL string or the type handlers
        should result in a different cache key.

        The cache key returned by this method is an instance of
        :class:`.CacheKey`, which consists of a tuple representing the
        cache key, as well as a list of :class:`.BindParameter` objects
        which are extracted from the expression.   While two expressions
        that produce identical cache key tuples will themselves generate
        identical SQL strings, the list of :class:`.BindParameter` objects
        indicates the bound values which may have different values in
        each one; these bound parameters must be consulted in order to
        execute the statement with the correct parameters.

        a :class:`_expression.ClauseElement` structure that does not implement
        a :meth:`._gen_cache_key` method and does not implement a
        :attr:`.traverse_internals` attribute will not be cacheable; when
        such an element is embedded into a larger structure, this method
        will return None, indicating no cache key is available.

        """

        bindparams = []

        _anon_map = anon_map()
        key = self._gen_cache_key(_anon_map, bindparams)
        if NO_CACHE in _anon_map:
            return None
        else:
            return CacheKey(key, bindparams)

    @classmethod
    def _generate_cache_key_for_object(cls, obj):
        bindparams = []

        _anon_map = anon_map()
        key = obj._gen_cache_key(_anon_map, bindparams)
        if NO_CACHE in _anon_map:
            return None
        else:
            return CacheKey(key, bindparams)


class MemoizedHasCacheKey(HasCacheKey, HasMemoized):
    @HasMemoized.memoized_instancemethod
    def _generate_cache_key(self):
        return HasCacheKey._generate_cache_key(self)


class CacheKey(namedtuple("CacheKey", ["key", "bindparams"])):
    def __hash__(self):
        """CacheKey itself is not hashable - hash the .key portion"""

        return None

    def to_offline_string(self, statement_cache, statement, parameters):
        """Generate an "offline string" form of this :class:`.CacheKey`

        The "offline string" is basically the string SQL for the
        statement plus a repr of the bound parameter values in series.
        Whereas the :class:`.CacheKey` object is dependent on in-memory
        identities in order to work as a cache key, the "offline" version
        is suitable for a cache that will work for other processes as well.

        The given ``statement_cache`` is a dictionary-like object where the
        string form of the statement itself will be cached.  This dictionary
        should be in a longer lived scope in order to reduce the time spent
        stringifying statements.


        """
        if self.key not in statement_cache:
            statement_cache[self.key] = sql_str = str(statement)
        else:
            sql_str = statement_cache[self.key]

        return repr(
            (
                sql_str,
                tuple(
                    parameters.get(bindparam.key, bindparam.value)
                    for bindparam in self.bindparams
                ),
            )
        )

    def __eq__(self, other):
        return self.key == other.key

    def _whats_different(self, other):

        k1 = self.key
        k2 = other.key

        stack = []
        pickup_index = 0
        while True:
            s1, s2 = k1, k2
            for idx in stack:
                s1 = s1[idx]
                s2 = s2[idx]

            for idx, (e1, e2) in enumerate(util.zip_longest(s1, s2)):
                if idx < pickup_index:
                    continue
                if e1 != e2:
                    if isinstance(e1, tuple) and isinstance(e2, tuple):
                        stack.append(idx)
                        break
                    else:
                        yield "key%s[%d]:  %s != %s" % (
                            "".join("[%d]" % id_ for id_ in stack),
                            idx,
                            e1,
                            e2,
                        )
            else:
                pickup_index = stack.pop(-1)
                break

    def _diff(self, other):
        return ", ".join(self._whats_different(other))

    def __str__(self):
        stack = [self.key]

        output = []
        sentinel = object()
        indent = -1
        while stack:
            elem = stack.pop(0)
            if elem is sentinel:
                output.append((" " * (indent * 2)) + "),")
                indent -= 1
            elif isinstance(elem, tuple):
                if not elem:
                    output.append((" " * ((indent + 1) * 2)) + "()")
                else:
                    indent += 1
                    stack = list(elem) + [sentinel] + stack
                    output.append((" " * (indent * 2)) + "(")
            else:
                if isinstance(elem, HasCacheKey):
                    repr_ = "<%s object at %s>" % (
                        type(elem).__name__,
                        hex(id(elem)),
                    )
                else:
                    repr_ = repr(elem)
                output.append((" " * (indent * 2)) + "  " + repr_ + ", ")

        return "CacheKey(key=%s)" % ("\n".join(output),)

    def _generate_param_dict(self):
        """used for testing"""

        from .compiler import prefix_anon_map

        _anon_map = prefix_anon_map()
        return {b.key % _anon_map: b.effective_value for b in self.bindparams}


def _clone(element, **kw):
    return element._clone()


class _CacheKey(ExtendedInternalTraversal):
    # very common elements are inlined into the main _get_cache_key() method
    # to produce a dramatic savings in Python function call overhead

    visit_has_cache_key = visit_clauseelement = CALL_GEN_CACHE_KEY
    visit_clauseelement_list = InternalTraversal.dp_clauseelement_list
    visit_annotations_key = InternalTraversal.dp_annotations_key
    visit_clauseelement_tuple = InternalTraversal.dp_clauseelement_tuple

    visit_string = (
        visit_boolean
    ) = visit_operator = visit_plain_obj = CACHE_IN_PLACE
    visit_statement_hint_list = CACHE_IN_PLACE
    visit_type = STATIC_CACHE_KEY
    visit_anon_name = ANON_NAME

    visit_propagate_attrs = PROPAGATE_ATTRS

    def visit_inspectable(self, attrname, obj, parent, anon_map, bindparams):
        return (attrname, inspect(obj)._gen_cache_key(anon_map, bindparams))

    def visit_string_list(self, attrname, obj, parent, anon_map, bindparams):
        return tuple(obj)

    def visit_multi(self, attrname, obj, parent, anon_map, bindparams):
        return (
            attrname,
            obj._gen_cache_key(anon_map, bindparams)
            if isinstance(obj, HasCacheKey)
            else obj,
        )

    def visit_multi_list(self, attrname, obj, parent, anon_map, bindparams):
        return (
            attrname,
            tuple(
                elem._gen_cache_key(anon_map, bindparams)
                if isinstance(elem, HasCacheKey)
                else elem
                for elem in obj
            ),
        )

    def visit_has_cache_key_tuples(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()
        return (
            attrname,
            tuple(
                tuple(
                    elem._gen_cache_key(anon_map, bindparams)
                    for elem in tup_elem
                )
                for tup_elem in obj
            ),
        )

    def visit_has_cache_key_list(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()
        return (
            attrname,
            tuple(elem._gen_cache_key(anon_map, bindparams) for elem in obj),
        )

    visit_executable_options = visit_has_cache_key_list

    def visit_inspectable_list(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return self.visit_has_cache_key_list(
            attrname, [inspect(o) for o in obj], parent, anon_map, bindparams
        )

    def visit_clauseelement_tuples(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return self.visit_has_cache_key_tuples(
            attrname, obj, parent, anon_map, bindparams
        )

    def visit_fromclause_ordered_set(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()
        return (
            attrname,
            tuple([elem._gen_cache_key(anon_map, bindparams) for elem in obj]),
        )

    def visit_clauseelement_unordered_set(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()
        cache_keys = [
            elem._gen_cache_key(anon_map, bindparams) for elem in obj
        ]
        return (
            attrname,
            tuple(
                sorted(cache_keys)
            ),  # cache keys all start with (id_, class)
        )

    def visit_named_ddl_element(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return (attrname, obj.name)

    def visit_prefix_sequence(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()

        return (
            attrname,
            tuple(
                [
                    (clause._gen_cache_key(anon_map, bindparams), strval)
                    for clause, strval in obj
                ]
            ),
        )

    def visit_setup_join_tuple(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        is_legacy = "legacy" in attrname

        return tuple(
            (
                target
                if is_legacy and isinstance(target, str)
                else target._gen_cache_key(anon_map, bindparams),
                onclause
                if is_legacy and isinstance(onclause, str)
                else onclause._gen_cache_key(anon_map, bindparams)
                if onclause is not None
                else None,
                from_._gen_cache_key(anon_map, bindparams)
                if from_ is not None
                else None,
                tuple([(key, flags[key]) for key in sorted(flags)]),
            )
            for (target, onclause, from_, flags) in obj
        )

    def visit_table_hint_list(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        if not obj:
            return ()

        return (
            attrname,
            tuple(
                [
                    (
                        clause._gen_cache_key(anon_map, bindparams),
                        dialect_name,
                        text,
                    )
                    for (clause, dialect_name), text in obj.items()
                ]
            ),
        )

    def visit_plain_dict(self, attrname, obj, parent, anon_map, bindparams):
        return (attrname, tuple([(key, obj[key]) for key in sorted(obj)]))

    def visit_dialect_options(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return (
            attrname,
            tuple(
                (
                    dialect_name,
                    tuple(
                        [
                            (key, obj[dialect_name][key])
                            for key in sorted(obj[dialect_name])
                        ]
                    ),
                )
                for dialect_name in sorted(obj)
            ),
        )

    def visit_string_clauseelement_dict(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return (
            attrname,
            tuple(
                (key, obj[key]._gen_cache_key(anon_map, bindparams))
                for key in sorted(obj)
            ),
        )

    def visit_string_multi_dict(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return (
            attrname,
            tuple(
                (
                    key,
                    value._gen_cache_key(anon_map, bindparams)
                    if isinstance(value, HasCacheKey)
                    else value,
                )
                for key, value in [(key, obj[key]) for key in sorted(obj)]
            ),
        )

    def visit_fromclause_canonical_column_collection(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        # inlining into the internals of ColumnCollection
        return (
            attrname,
            tuple(
                col._gen_cache_key(anon_map, bindparams)
                for k, col in obj._collection
            ),
        )

    def visit_unknown_structure(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        anon_map[NO_CACHE] = True
        return ()

    def visit_dml_ordered_values(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        return (
            attrname,
            tuple(
                (
                    key._gen_cache_key(anon_map, bindparams)
                    if hasattr(key, "__clause_element__")
                    else key,
                    value._gen_cache_key(anon_map, bindparams),
                )
                for key, value in obj
            ),
        )

    def visit_dml_values(self, attrname, obj, parent, anon_map, bindparams):
        if py37:
            # in py37 we can assume two dictionaries created in the same
            # insert ordering will retain that sorting
            return (
                attrname,
                tuple(
                    (
                        k._gen_cache_key(anon_map, bindparams)
                        if hasattr(k, "__clause_element__")
                        else k,
                        obj[k]._gen_cache_key(anon_map, bindparams),
                    )
                    for k in obj
                ),
            )
        else:
            expr_values = {k for k in obj if hasattr(k, "__clause_element__")}
            if expr_values:
                # expr values can't be sorted deterministically right now,
                # so no cache
                anon_map[NO_CACHE] = True
                return ()

            str_values = expr_values.symmetric_difference(obj)

            return (
                attrname,
                tuple(
                    (k, obj[k]._gen_cache_key(anon_map, bindparams))
                    for k in sorted(str_values)
                ),
            )

    def visit_dml_multi_values(
        self, attrname, obj, parent, anon_map, bindparams
    ):
        # multivalues are simply not cacheable right now
        anon_map[NO_CACHE] = True
        return ()


_cache_key_traversal_visitor = _CacheKey()


class HasCopyInternals(object):
    def _clone(self, **kw):
        raise NotImplementedError()

    def _copy_internals(self, omit_attrs=(), **kw):
        """Reassign internal elements to be clones of themselves.

        Called during a copy-and-traverse operation on newly
        shallow-copied elements to create a deep copy.

        The given clone function should be used, which may be applying
        additional transformations to the element (i.e. replacement
        traversal, cloned traversal, annotations).

        """

        try:
            traverse_internals = self._traverse_internals
        except AttributeError:
            # user-defined classes may not have a _traverse_internals
            return

        for attrname, obj, meth in _copy_internals.run_generated_dispatch(
            self, traverse_internals, "_generated_copy_internals_traversal"
        ):
            if attrname in omit_attrs:
                continue

            if obj is not None:

                result = meth(attrname, self, obj, **kw)
                if result is not None:
                    setattr(self, attrname, result)


class _CopyInternals(InternalTraversal):
    """Generate a _copy_internals internal traversal dispatch for classes
    with a _traverse_internals collection."""

    def visit_clauseelement(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return clone(element, **kw)

    def visit_clauseelement_list(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return [clone(clause, **kw) for clause in element]

    def visit_clauseelement_tuple(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return tuple([clone(clause, **kw) for clause in element])

    def visit_executable_options(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return tuple([clone(clause, **kw) for clause in element])

    def visit_clauseelement_unordered_set(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return {clone(clause, **kw) for clause in element}

    def visit_clauseelement_tuples(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return [
            tuple(clone(tup_elem, **kw) for tup_elem in elem)
            for elem in element
        ]

    def visit_string_clauseelement_dict(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return dict(
            (key, clone(value, **kw)) for key, value in element.items()
        )

    def visit_setup_join_tuple(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return tuple(
            (
                clone(target, **kw) if target is not None else None,
                clone(onclause, **kw) if onclause is not None else None,
                clone(from_, **kw) if from_ is not None else None,
                flags,
            )
            for (target, onclause, from_, flags) in element
        )

    def visit_dml_ordered_values(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        # sequence of 2-tuples
        return [
            (
                clone(key, **kw)
                if hasattr(key, "__clause_element__")
                else key,
                clone(value, **kw),
            )
            for key, value in element
        ]

    def visit_dml_values(self, attrname, parent, element, clone=_clone, **kw):
        return {
            (
                clone(key, **kw) if hasattr(key, "__clause_element__") else key
            ): clone(value, **kw)
            for key, value in element.items()
        }

    def visit_dml_multi_values(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        # sequence of sequences, each sequence contains a list/dict/tuple

        def copy(elem):
            if isinstance(elem, (list, tuple)):
                return [
                    clone(value, **kw)
                    if hasattr(value, "__clause_element__")
                    else value
                    for value in elem
                ]
            elif isinstance(elem, dict):
                return {
                    (
                        clone(key, **kw)
                        if hasattr(key, "__clause_element__")
                        else key
                    ): (
                        clone(value, **kw)
                        if hasattr(value, "__clause_element__")
                        else value
                    )
                    for key, value in elem.items()
                }
            else:
                # TODO: use abc classes
                assert False

        return [
            [copy(sub_element) for sub_element in sequence]
            for sequence in element
        ]

    def visit_propagate_attrs(
        self, attrname, parent, element, clone=_clone, **kw
    ):
        return element


_copy_internals = _CopyInternals()


def _flatten_clauseelement(element):
    while hasattr(element, "__clause_element__") and not getattr(
        element, "is_clause_element", False
    ):
        element = element.__clause_element__()

    return element


class _GetChildren(InternalTraversal):
    """Generate a _children_traversal internal traversal dispatch for classes
    with a _traverse_internals collection."""

    def visit_has_cache_key(self, element, **kw):
        # the GetChildren traversal refers explicitly to ClauseElement
        # structures.  Within these, a plain HasCacheKey is not a
        # ClauseElement, so don't include these.
        return ()

    def visit_clauseelement(self, element, **kw):
        return (element,)

    def visit_clauseelement_list(self, element, **kw):
        return element

    def visit_clauseelement_tuple(self, element, **kw):
        return element

    def visit_clauseelement_tuples(self, element, **kw):
        return itertools.chain.from_iterable(element)

    def visit_fromclause_canonical_column_collection(self, element, **kw):
        return ()

    def visit_string_clauseelement_dict(self, element, **kw):
        return element.values()

    def visit_fromclause_ordered_set(self, element, **kw):
        return element

    def visit_clauseelement_unordered_set(self, element, **kw):
        return element

    def visit_setup_join_tuple(self, element, **kw):
        for (target, onclause, from_, flags) in element:
            if from_ is not None:
                yield from_

            if not isinstance(target, str):
                yield _flatten_clauseelement(target)

            if onclause is not None and not isinstance(onclause, str):
                yield _flatten_clauseelement(onclause)

    def visit_dml_ordered_values(self, element, **kw):
        for k, v in element:
            if hasattr(k, "__clause_element__"):
                yield k
            yield v

    def visit_dml_values(self, element, **kw):
        expr_values = {k for k in element if hasattr(k, "__clause_element__")}
        str_values = expr_values.symmetric_difference(element)

        for k in sorted(str_values):
            yield element[k]
        for k in expr_values:
            yield k
            yield element[k]

    def visit_dml_multi_values(self, element, **kw):
        return ()

    def visit_propagate_attrs(self, element, **kw):
        return ()


_get_children = _GetChildren()


@util.preload_module("sqlalchemy.sql.elements")
def _resolve_name_for_compare(element, name, anon_map, **kw):
    if isinstance(name, util.preloaded.sql_elements._anonymous_label):
        name = name.apply_map(anon_map)

    return name


class anon_map(dict):
    """A map that creates new keys for missing key access.

    Produces an incrementing sequence given a series of unique keys.

    This is similar to the compiler prefix_anon_map class although simpler.

    Inlines the approach taken by :class:`sqlalchemy.util.PopulateDict` which
    is otherwise usually used for this type of operation.

    """

    def __init__(self):
        self.index = 0

    def __missing__(self, key):
        self[key] = val = str(self.index)
        self.index += 1
        return val


class TraversalComparatorStrategy(InternalTraversal, util.MemoizedSlots):
    __slots__ = "stack", "cache", "anon_map"

    def __init__(self):
        self.stack = deque()
        self.cache = set()

    def _memoized_attr_anon_map(self):
        return (anon_map(), anon_map())

    def compare(self, obj1, obj2, **kw):
        stack = self.stack
        cache = self.cache

        compare_annotations = kw.get("compare_annotations", False)

        stack.append((obj1, obj2))

        while stack:
            left, right = stack.popleft()

            if left is right:
                continue
            elif left is None or right is None:
                # we know they are different so no match
                return False
            elif (left, right) in cache:
                continue
            cache.add((left, right))

            visit_name = left.__visit_name__
            if visit_name != right.__visit_name__:
                return False

            meth = getattr(self, "compare_%s" % visit_name, None)

            if meth:
                attributes_compared = meth(left, right, **kw)
                if attributes_compared is COMPARE_FAILED:
                    return False
                elif attributes_compared is SKIP_TRAVERSE:
                    continue

                # attributes_compared is returned as a list of attribute
                # names that were "handled" by the comparison method above.
                # remaining attribute names in the _traverse_internals
                # will be compared.
            else:
                attributes_compared = ()

            for (
                (left_attrname, left_visit_sym),
                (right_attrname, right_visit_sym),
            ) in util.zip_longest(
                left._traverse_internals,
                right._traverse_internals,
                fillvalue=(None, None),
            ):
                if not compare_annotations and (
                    (left_attrname == "_annotations")
                    or (right_attrname == "_annotations")
                ):
                    continue

                if (
                    left_attrname != right_attrname
                    or left_visit_sym is not right_visit_sym
                ):
                    return False
                elif left_attrname in attributes_compared:
                    continue

                dispatch = self.dispatch(left_visit_sym)
                left_child = operator.attrgetter(left_attrname)(left)
                right_child = operator.attrgetter(right_attrname)(right)
                if left_child is None:
                    if right_child is not None:
                        return False
                    else:
                        continue

                comparison = dispatch(
                    left_attrname, left, left_child, right, right_child, **kw
                )
                if comparison is COMPARE_FAILED:
                    return False

        return True

    def compare_inner(self, obj1, obj2, **kw):
        comparator = self.__class__()
        return comparator.compare(obj1, obj2, **kw)

    def visit_has_cache_key(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        if left._gen_cache_key(self.anon_map[0], []) != right._gen_cache_key(
            self.anon_map[1], []
        ):
            return COMPARE_FAILED

    def visit_propagate_attrs(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return self.compare_inner(
            left.get("plugin_subject", None), right.get("plugin_subject", None)
        )

    def visit_has_cache_key_list(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for l, r in util.zip_longest(left, right, fillvalue=None):
            if l._gen_cache_key(self.anon_map[0], []) != r._gen_cache_key(
                self.anon_map[1], []
            ):
                return COMPARE_FAILED

    visit_executable_options = visit_has_cache_key_list

    def visit_clauseelement(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        self.stack.append((left, right))

    def visit_fromclause_canonical_column_collection(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for lcol, rcol in util.zip_longest(left, right, fillvalue=None):
            self.stack.append((lcol, rcol))

    def visit_fromclause_derived_column_collection(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        pass

    def visit_string_clauseelement_dict(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for lstr, rstr in util.zip_longest(
            sorted(left), sorted(right), fillvalue=None
        ):
            if lstr != rstr:
                return COMPARE_FAILED
            self.stack.append((left[lstr], right[rstr]))

    def visit_clauseelement_tuples(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for ltup, rtup in util.zip_longest(left, right, fillvalue=None):
            if ltup is None or rtup is None:
                return COMPARE_FAILED

            for l, r in util.zip_longest(ltup, rtup, fillvalue=None):
                self.stack.append((l, r))

    def visit_clauseelement_list(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for l, r in util.zip_longest(left, right, fillvalue=None):
            self.stack.append((l, r))

    def visit_clauseelement_tuple(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for l, r in util.zip_longest(left, right, fillvalue=None):
            self.stack.append((l, r))

    def _compare_unordered_sequences(self, seq1, seq2, **kw):
        if seq1 is None:
            return seq2 is None

        completed = set()
        for clause in seq1:
            for other_clause in set(seq2).difference(completed):
                if self.compare_inner(clause, other_clause, **kw):
                    completed.add(other_clause)
                    break
        return len(completed) == len(seq1) == len(seq2)

    def visit_clauseelement_unordered_set(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return self._compare_unordered_sequences(left, right, **kw)

    def visit_fromclause_ordered_set(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for l, r in util.zip_longest(left, right, fillvalue=None):
            self.stack.append((l, r))

    def visit_string(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_string_list(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_anon_name(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return _resolve_name_for_compare(
            left_parent, left, self.anon_map[0], **kw
        ) == _resolve_name_for_compare(
            right_parent, right, self.anon_map[1], **kw
        )

    def visit_boolean(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_operator(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left is right

    def visit_type(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left._compare_type_affinity(right)

    def visit_plain_dict(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_dialect_options(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_annotations_key(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        if left and right:
            return (
                left_parent._annotations_cache_key
                == right_parent._annotations_cache_key
            )
        else:
            return left == right

    def visit_plain_obj(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_named_ddl_element(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        if left is None:
            if right is not None:
                return COMPARE_FAILED

        return left.name == right.name

    def visit_prefix_sequence(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for (l_clause, l_str), (r_clause, r_str) in util.zip_longest(
            left, right, fillvalue=(None, None)
        ):
            if l_str != r_str:
                return COMPARE_FAILED
            else:
                self.stack.append((l_clause, r_clause))

    def visit_setup_join_tuple(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        # TODO: look at attrname for "legacy_join" and use different structure
        for (
            (l_target, l_onclause, l_from, l_flags),
            (r_target, r_onclause, r_from, r_flags),
        ) in util.zip_longest(left, right, fillvalue=(None, None, None, None)):
            if l_flags != r_flags:
                return COMPARE_FAILED
            self.stack.append((l_target, r_target))
            self.stack.append((l_onclause, r_onclause))
            self.stack.append((l_from, r_from))

    def visit_table_hint_list(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        left_keys = sorted(left, key=lambda elem: (elem[0].fullname, elem[1]))
        right_keys = sorted(
            right, key=lambda elem: (elem[0].fullname, elem[1])
        )
        for (ltable, ldialect), (rtable, rdialect) in util.zip_longest(
            left_keys, right_keys, fillvalue=(None, None)
        ):
            if ldialect != rdialect:
                return COMPARE_FAILED
            elif left[(ltable, ldialect)] != right[(rtable, rdialect)]:
                return COMPARE_FAILED
            else:
                self.stack.append((ltable, rtable))

    def visit_statement_hint_list(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        return left == right

    def visit_unknown_structure(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        raise NotImplementedError()

    def visit_dml_ordered_values(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        # sequence of tuple pairs

        for (lk, lv), (rk, rv) in util.zip_longest(
            left, right, fillvalue=(None, None)
        ):
            if not self._compare_dml_values_or_ce(lk, rk, **kw):
                return COMPARE_FAILED

    def _compare_dml_values_or_ce(self, lv, rv, **kw):
        lvce = hasattr(lv, "__clause_element__")
        rvce = hasattr(rv, "__clause_element__")
        if lvce != rvce:
            return False
        elif lvce and not self.compare_inner(lv, rv, **kw):
            return False
        elif not lvce and lv != rv:
            return False
        elif not self.compare_inner(lv, rv, **kw):
            return False

        return True

    def visit_dml_values(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        if left is None or right is None or len(left) != len(right):
            return COMPARE_FAILED

        if isinstance(left, collections_abc.Sequence):
            for lv, rv in zip(left, right):
                if not self._compare_dml_values_or_ce(lv, rv, **kw):
                    return COMPARE_FAILED
        elif isinstance(right, collections_abc.Sequence):
            return COMPARE_FAILED
        elif py37:
            # dictionaries guaranteed to support insert ordering in
            # py37 so that we can compare the keys in order.  without
            # this, we can't compare SQL expression keys because we don't
            # know which key is which
            for (lk, lv), (rk, rv) in zip(left.items(), right.items()):
                if not self._compare_dml_values_or_ce(lk, rk, **kw):
                    return COMPARE_FAILED
                if not self._compare_dml_values_or_ce(lv, rv, **kw):
                    return COMPARE_FAILED
        else:
            for lk in left:
                lv = left[lk]

                if lk not in right:
                    return COMPARE_FAILED
                rv = right[lk]

                if not self._compare_dml_values_or_ce(lv, rv, **kw):
                    return COMPARE_FAILED

    def visit_dml_multi_values(
        self, attrname, left_parent, left, right_parent, right, **kw
    ):
        for lseq, rseq in util.zip_longest(left, right, fillvalue=None):
            if lseq is None or rseq is None:
                return COMPARE_FAILED

            for ld, rd in util.zip_longest(lseq, rseq, fillvalue=None):
                if (
                    self.visit_dml_values(
                        attrname, left_parent, ld, right_parent, rd, **kw
                    )
                    is COMPARE_FAILED
                ):
                    return COMPARE_FAILED

    def compare_clauselist(self, left, right, **kw):
        if left.operator is right.operator:
            if operators.is_associative(left.operator):
                if self._compare_unordered_sequences(
                    left.clauses, right.clauses, **kw
                ):
                    return ["operator", "clauses"]
                else:
                    return COMPARE_FAILED
            else:
                return ["operator"]
        else:
            return COMPARE_FAILED

    def compare_binary(self, left, right, **kw):
        if left.operator == right.operator:
            if operators.is_commutative(left.operator):
                if (
                    self.compare_inner(left.left, right.left, **kw)
                    and self.compare_inner(left.right, right.right, **kw)
                ) or (
                    self.compare_inner(left.left, right.right, **kw)
                    and self.compare_inner(left.right, right.left, **kw)
                ):
                    return ["operator", "negate", "left", "right"]
                else:
                    return COMPARE_FAILED
            else:
                return ["operator", "negate"]
        else:
            return COMPARE_FAILED

    def compare_bindparam(self, left, right, **kw):
        compare_keys = kw.pop("compare_keys", True)
        compare_values = kw.pop("compare_values", True)

        if compare_values:
            omit = []
        else:
            # this means, "skip these, we already compared"
            omit = ["callable", "value"]

        if not compare_keys:
            omit.append("key")

        return omit


class ColIdentityComparatorStrategy(TraversalComparatorStrategy):
    def compare_column_element(
        self, left, right, use_proxies=True, equivalents=(), **kw
    ):
        """Compare ColumnElements using proxies and equivalent collections.

        This is a comparison strategy specific to the ORM.
        """

        to_compare = (right,)
        if equivalents and right in equivalents:
            to_compare = equivalents[right].union(to_compare)

        for oth in to_compare:
            if use_proxies and left.shares_lineage(oth):
                return SKIP_TRAVERSE
            elif hash(left) == hash(right):
                return SKIP_TRAVERSE
        else:
            return COMPARE_FAILED

    def compare_column(self, left, right, **kw):
        return self.compare_column_element(left, right, **kw)

    def compare_label(self, left, right, **kw):
        return self.compare_column_element(left, right, **kw)

    def compare_table(self, left, right, **kw):
        # tables compare on identity, since it's not really feasible to
        # compare them column by column with the above rules
        return SKIP_TRAVERSE if left is right else COMPARE_FAILED
