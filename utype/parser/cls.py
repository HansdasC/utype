from .base import BaseParser
from .func import FunctionParser
from ..utils.compat import is_classvar, is_final
from ..utils.functional import pop
from .field import ParserField
from typing import Callable, Dict
import inspect
from ..utils.transform import register_transformer, TypeTransformer
from collections.abc import Mapping
from .options import RuntimeOptions
from functools import partial
import warnings
from types import FunctionType


__all__ = ['ClassParser']


class ClassParser(BaseParser):
    IGNORE_ATTR_TYPES = (staticmethod, classmethod, FunctionType, type)
    # if these type not having annotation, we will not recognize them as field
    function_parser_cls = FunctionParser
    fields: Dict[str, ParserField]

    def __init__(self, obj, *args, **kwargs):
        if not inspect.isclass(obj):
            raise TypeError(f"{self.__class__}: object need to be a class, got {obj}")
        super().__init__(obj, *args, **kwargs)
        self.name = getattr(self.obj, "__qualname__", self.obj.__name__)
        self.init_parser = None

    def setup(self):
        self.generate_from_bases()
        super().setup()

    def validate_class_field_name(self, name: str):
        if not self.validate_field_name(name):
            return False
        for base in self.obj.__bases__:
            if base is object:
                continue
            annotations = getattr(base, "__annotations__", None)
            if annotations:
                # maybe object
                annotation = annotations.get(name)
                if annotation:
                    if is_final(annotation):
                        raise TypeError(
                            f"field: {repr(name)} was declared as Final in {base}, "
                            f"so {self.obj} cannot annotate it again"
                        )

            attr = getattr(base, name, None)
            if self.is_class_internals(
                attr,
                attname=name,
                class_qualname=base.__qualname__
            ):
                raise TypeError(
                    f"field: {repr(name)} was declared in {base}, "
                    f"so {self.obj} cannot annotate it as a field"
                )
        return True

    @classmethod
    def is_class_internals(cls, attr, attname: str, class_qualname: str = None):
        if isinstance(attr, (staticmethod, classmethod)):
            return True
        if inspect.ismethoddescriptor(attr):
            # like method_descriptor
            return True
        qualname: str = getattr(attr, "__qualname__", None)
        name: str = getattr(attr, "__name__", None)
        if name and qualname:
            if not class_qualname:
                # loosely check
                return attname == name and '.' in qualname

            if attname == name and qualname.startswith(f"{class_qualname}."):
                return True
        return False

    def generate_fields(self):
        exclude_vars = self.exclude_vars
        fields = []

        annotations = self.obj.__dict__.get("__annotations__", {})
        # get annotations from __dict__
        # because if base has annotations and sub does not
        # it will directly use the annotations attr of base's

        for key, annotation in annotations.items():
            if (
                not self.validate_class_field_name(key)
                or is_classvar(annotation)
                # or is_final(annotation)
            ):
                exclude_vars.add(key)
                continue
            default = self.obj.__dict__.get(key, ...)
            if annotation is None:
                # a: None
                # a: Optional[None]
                # a: Union[None]
                annotation = type(None)
                # to make a difference to annotation=None
            fields.append(
                self.schema_field_cls.generate(
                    attname=key,
                    annotation=annotation,
                    default=default,
                    global_vars=self.globals,
                    forward_refs=self.forward_refs,
                    options=self.options,
                )
            )

        for key, attr in self.obj.__dict__.items():
            if key in annotations:
                continue
            if (
                attr is ...
                # if this attr is a field in bases, this means to exclude this field in current class
                # otherwise this attr declared that this field is never take from input
                # or isinstance(attr, property)
                or self.is_class_internals(
                    attr, attname=key, class_qualname=self.obj_name
                )
                or isinstance(attr, self.IGNORE_ATTR_TYPES)
                # check class field name at last
                # because this will check bases internals trying to find illegal override
                or not self.validate_class_field_name(key)
            ):
                exclude_vars.add(key)
                continue
            if key in exclude_vars:
                continue
            fields.append(
                self.schema_field_cls.generate(
                    attname=key,
                    annotation=None,
                    default=attr,
                    global_vars=self.globals,
                    forward_refs=self.forward_refs,
                    options=self.options,
                )
            )

        field_map = {}
        for field in fields:
            if field.name in field_map:
                raise ValueError(
                    f"{self.obj}: field name: {repr(field.name)} conflicted at "
                    f"{field}, {field_map[field.name]}"
                )
            field_map[field.name] = field
        self.fields.update(field_map)

    def generate_from_bases(self):
        fields = {}
        alias_map = {}
        attr_alias_map = {}
        case_insensitive_names = set()
        exclude_vars = set()
        option_list = []

        for base in reversed(self.obj.__bases__):  # according to MRO
            if not isinstance(base, type(self.obj)) or base is object:
                continue
            parser = self.apply_for(base)  # should use cache
            if not parser.options.vacuum:
                option_list.append(parser.options)

            fields.update(parser.fields)

            exclude_vars.update(parser.exclude_vars)
            alias_map.update(parser.field_alias_map)
            attr_alias_map.update(parser.attr_alias_map)
            case_insensitive_names.update(parser.case_insensitive_names)

        cls_options = self.options  # add current cls options
        if cls_options:
            option_list.append(cls_options)

        self.options = self.options_cls.generate_from(*option_list)
        self.fields = fields
        self.exclude_vars = exclude_vars
        self.field_alias_map = alias_map
        self.attr_alias_map = attr_alias_map
        self.case_insensitive_names = case_insensitive_names

    def make_setter(self, field: ParserField, post_setattr=None):
        def setter(_obj_self: object, value):
            if self.options.immutable or field.immutable:
                raise AttributeError(
                    f"{self.name}: "
                    f"Attempt to set immutable attribute: [{repr(field.attname)}]"
                )

            options = self.options.make_runtime(_obj_self.__class__, force_error=True)
            value = field.parse_value(value, options=options)
            _obj_self.__dict__[field.attname] = value
            if callable(post_setattr):
                post_setattr(_obj_self, field, value, options)
        return setter

    def make_deleter(self, field: ParserField, post_delattr=None):
        def deleter(_obj_self: object):
            if self.options.immutable or field.immutable:
                raise AttributeError(
                    f"{self.name}: "
                    f"Attempt to set immutable attribute: [{repr(field.attname)}]"
                )

            options = self.options.make_runtime(_obj_self.__class__, force_error=True)
            if field.is_required(options):
                raise AttributeError(
                    f"{self.name}: Attempt to delete required schema key: {repr(field.attname)}"
                )

            if field.attname not in _obj_self.__dict__:
                raise AttributeError(
                    f"{self.name}: Attempt to delete nonexistent key: {repr(field.attname)}"
                )

            _obj_self.__dict__.pop(field.attname)

            if callable(post_delattr):
                post_delattr(_obj_self, field, options)
        return deleter

    def make_getter(self, field: ParserField):
        def getter(_obj_self: object):
            if field.attname not in _obj_self.__dict__:
                raise AttributeError(
                    f"{self.name}: {repr(field.attname)} not provided in schema"
                )
            return _obj_self.__dict__[field.attname]
        return getter

    def assign_properties(self,
                          getter: Callable = None,
                          setter: Callable = None,
                          deleter: Callable = None,
                          post_setattr: Callable = None,
                          post_delattr: Callable = None):

        for key, field in self.fields.items():
            if field.property:
                continue

            if getter:
                field_getter = partial(getter, field=field)
            else:
                field_getter = self.make_getter(field)
            if setter:
                field_setter = partial(setter, field=field)
            else:
                field_setter = self.make_setter(field, post_setattr=post_setattr)
            if deleter:
                field_deleter = partial(deleter, field=field)
            else:
                field_deleter = self.make_deleter(field, post_delattr=post_delattr)

            for f in (field_getter, field_setter, field_deleter):
                f.__name__ = field.attname

            prop = property(
                fget=field_getter,
                fset=field_setter,
                fdel=field_deleter
            )
            # prop.__field__ = field        # cannot set attribute to @property
            setattr(self.obj, field.attname, prop)

    def get_parser(self, obj_self: object):
        if self.obj == obj_self.__class__:
            return self
        return self.resolve_parser(obj_self.__class__)

    def _make_method(self, func: Callable, name: str = None):
        if name:
            func.__name__ = name
        else:
            name = func.__name__

        if name in self.obj.__dict__:
            # already declared
            return False
        attr_func = getattr(self.obj, name, None)
        if hasattr(attr_func, '__parser__'):
            # already inherited
            return False
        func.__parser__ = self
        setattr(self.obj, name, func)
        return True

    def make_contains(self, output_only: bool = False):
        def __contains__(_obj_self, item: str):
            parser = self.get_parser(_obj_self)
            field = parser.get_field(item)
            if not field:
                return False
            if field.attname not in _obj_self.__dict__:
                return False
            if not output_only:
                return True
            if field.no_output(_obj_self.__dict__[field.attname], options=parser.options):
                return False
            return True

        self._make_method(__contains__)

    def make_eq(self):
        def __eq__(_obj_self, other):
            if not isinstance(other, _obj_self.__class__):
                return False
            return _obj_self.__dict__ == other.__dict__
        self._make_method(__eq__)

    def make_repr(self, ignore_str: bool = False):
        def __repr__(_obj_self):
            parser = self.get_parser(_obj_self)
            items = []
            for key, val in _obj_self.__dict__.items():
                field = parser.get_field(key)
                if not field:
                    continue
                items.append(f"{field.attname}={field.repr_value(val)}")
            values = ", ".join(items)
            return f"{parser.name}({values})"
        self._make_method(__repr__)

        if not ignore_str:
            def __str__(_obj_self):
                return _obj_self.__repr__()
            self._make_method(__str__)

    def set_attributes(self,
                       values: dict,
                       instance: object,
                       options: RuntimeOptions,
                       ):

        for key, value in list(values.items()):
            field = self.get_field(key)
            attname = key
            if field:
                if field.no_output(values[key], options=options):
                    values.pop(key)
                if field.property:
                    try:
                        field.property.fset(instance, values[key])  # call the original setter
                        # setattr(instance, field.attname, values[key])
                    except Exception as e:
                        error_option = field.get_on_error(options)
                        msg = f"@property: {repr(field.attname)} assign failed with error: {e}"
                        if error_option == options.THROW:
                            raise e.__class__(msg) from e
                        else:
                            warnings.warn(msg)
                    continue
                attname = field.attname

            # TODO: it seems redundant for Schema, so we just use it as a fallback for now
            # and work on it later if something went wrong
            instance.__dict__[attname] = value
            # set to __dict__ no matter field (maybe addition=True)

    def make_init(
        self,
        # init_super: bool = False,
        # allow_runtime: bool = False,
        # set_attributes: bool = True,
        # coerce_property: bool = False,
        no_parse: bool = False,
        post_init: Callable = None,
    ):

        init_func = getattr(self.obj, "__init__", None)

        init_parser = self.resolve_parser(init_func)
        if init_parser:
            # if init_func is already decorated like a Wrapper
            # we do not touch it either
            # case1: user use @utype.parse over the __init__ function
            # case2: base ClassParser has assigned the wrapped init with __parser__ attribute
            self.init_parser = init_parser
            return

        if not inspect.isfunction(init_func) or self.function_parser_cls.function_pass(init_func):
            # if __init__ is declared but passed, we still make a new one

            def __init__(_obj_self, **kwargs):
                parser = self.get_parser(_obj_self)
                options = parser.options.make_runtime(
                    parser.obj,
                    options=pop(kwargs, "__options__")  # if allow_runtime else None,
                )

                if no_parse:
                    values = kwargs
                else:
                    values = parser(kwargs, options=options)

                parser.set_attributes(values, _obj_self, options=options)

                if post_init:
                    post_init(_obj_self, values, options)

            __init__.__parser__ = self
        else:
            if not no_parse:
                self.init_parser = self.function_parser_cls.apply_for(init_func)
                __init__ = self.init_parser.wrap(parse_params=True, parse_result=False)
                __init__.__parser__ = self
                # wrapped function is not as same as parse.obj
            else:
                __init__ = init_func

        setattr(self.obj, "__init__", __init__)
        # self.obj.__dict__['__init__'] = __init__
        # the INPUT parser
        # we do not merge fields or options here
        # each part does there job
        # init just parse data as it declared and take it to initialize the class
        # class T(Schema):
        #     mul: int
        #     def __init__(self, a: float, b: int):
        #         super().__init__(mul=a * b)
        # we will make init_parser the "INPUT" parser

        return __init__


@register_transformer(
    attr="__parser__",
    detector=lambda cls: isinstance(getattr(cls, "__parser__", None), ClassParser),
)
def transform(transformer: TypeTransformer, data, cls):
    if not isinstance(data, Mapping):
        # {} dict instance is a instance of Mapping too
        if transformer.no_explicit_cast:
            raise TypeError(f"invalid input type for {cls}, should be dict or Mapping")
        else:
            data = transformer(data, dict)
    if not transformer.options.vacuum:
        parser: ClassParser = cls.__parser__
        if parser.options.allowed_runtime_options:
            # pass the runtime options
            data.update(__options__=transformer.options)
    return cls(**data)
