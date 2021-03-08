from tools.codegen.api import cpp
from tools.codegen.api.autograd import *
from tools.codegen.api.types import *
from tools.codegen.code_template import CodeTemplate
from tools.codegen.context import with_native_function
from tools.codegen.model import *
from typing import List, Optional, Sequence, Tuple


# See NOTE [ Autograd View Variables ] in variable.h for details.
# If you update list VIEW_FUNCTIONS or RETURNS_VIEWS_OF_INPUT,
# you **MUST** also update the public list of view ops accordingly in
# docs/source/tensor_view.rst. Note not all ATen functions are exposed to public,
# e.g alias & sparse_coo_tensor_with_dims_and_tensors.
#
# A map: function name => name of the argument that all outputs are view of

VIEW_FUNCTIONS_WITH_METADATA_CHANGE = ['view_as_real', 'view_as_complex']

VIEW_FUNCTIONS = {
    'numpy_T': 'self',
    'alias': 'self',
    'as_strided': 'self',
    'diagonal': 'self',
    'expand': 'self',
    'permute': 'self',
    'select': 'self',
    'slice': 'self',
    'split': 'self',
    'split_with_sizes': 'self',
    'squeeze': 'self',
    't': 'self',
    'transpose': 'self',
    'unfold': 'self',
    'unsqueeze': 'self',
    'flatten': 'self',
    'view': 'self',
    'unbind': 'self',
    '_indices': 'self',
    '_values': 'self',
    'indices': 'self',
    'values': 'self',
    # sparse_coo ctor output should really be views of both indices and values,
    # but we only supports making as view of a single variable, and indices is
    # discrete anyways.
    # FIXME: clone indices on construction.
    'sparse_coo_tensor_with_dims_and_tensors': 'values',
}

for key in VIEW_FUNCTIONS_WITH_METADATA_CHANGE:
    VIEW_FUNCTIONS[key] = 'self'

# Functions for which we use CreationMeta::MULTI_OUTPUT_SAFE. I.e., the ones for
# which inplace modification of outputs is being gradually deprecated.
MULTI_OUTPUT_SAFE_FUNCTIONS = {
    'split',
    'split_with_sizes',
}

# note: some VIEW_FUNCTIONS are just compositions of the view functions above
# this list contains both the root view functions and any that are purely composed
# of viewing functions, and is used by the JIT to determine when an operator
# may return a view of its inputs; however they may sometimes return a copy.
# (e.g. `contiguous`)
RETURNS_VIEWS_OF_INPUT = set(VIEW_FUNCTIONS.keys()).union({
    'chunk', 'detach', 'contiguous', 'reshape', 'reshape_as',
    'expand_as', 'view_as', 'real', 'imag', 'narrow', 'movedim',
    'tensor_split', 'swapdims', 'swapaxes'
})

ARRAYREF_TO_VEC = CodeTemplate("""\
auto ${vec} = ${arg}.vec();
""")

OPTIONAL_TO_VAL = CodeTemplate("""\
auto ${val} = ${arg}.value_or(${default});
""")

CALL_DISPATCH_VIA_NAMESPACE = CodeTemplate("""\
at::${api_name}(${unpacked_args})""")

CALL_DISPATCH_VIA_METHOD = CodeTemplate("""\
${var}.${api_name}(${unpacked_method_args})""")

SETUP_REPLAY_VIEW_IF_NOT_SUPPORT_AS_STRIDED_OR_VIEW_WITH_METADATA_CHANGE = CodeTemplate("""\
std::function<at::Tensor(const at::Tensor&)> func=nullptr;
if (${is_view_with_metadata_change} || !self.unsafeGetTensorImpl()->support_as_strided()) {
  ${replay_view_func}
}
""")

REPLAY_VIEW_LAMBDA_FUNC = CodeTemplate("""\
func = [=](const at::Tensor& ${input_base}) {
  return ${replay_view_call};
};
""")

# FIXME: Ideally these functions should be methods on Type class, but we have a
#        comment in codegen/model.py there saying these concepts are not well defined.
#        Thus we put a version that commonly used by autograd codegen here.
def is_tensor_type(t: Type) -> bool:
    # TODO: Should handle optional here?
    return t.is_tensor_like() and t.is_list_like() is None

def is_tensor_list_type(t: Type) -> bool:
    # TODO: Should handle optional here?
    return t.is_tensor_like() and t.is_list_like() is not None

UNPACK_TENSOR = CodeTemplate("""\
auto${ref} ${arg_name}_ = unpack${suffix}(${arg_name}, "${arg_name}", ${arg_pos});""")

@with_native_function
def unpack_args(f: NativeFunction) -> Tuple[List[str], List[Binding]]:
    body: List[str] = []
    unpacked_bindings: List[Binding] = []

    bindings = [r for a in f.func.schema_order_arguments()
                for r in cpp.argument(a,
                                      method=False,
                                      cpp_no_default_args=set(),
                                      faithful=False,
                                      has_tensor_options=False)]

    for i, binding in enumerate(bindings):
        assert not isinstance(binding.argument, SelfArgument)
        if isinstance(binding.argument, TensorOptionsArguments):
            raise RuntimeError("VariableKernel shouldn't take TensorOptions")

        is_nullable = binding.argument.type.is_nullable()
        if not binding.argument.type.is_tensor_like() or is_nullable:
            unpacked_bindings.append(binding)
            continue

        is_tensor_list = is_tensor_list_type(binding.argument.type)
        ref = (not is_nullable) and not is_tensor_list
        suffix = '_opt' if is_nullable and not is_tensor_list else ''
        body.append(UNPACK_TENSOR.substitute(
            arg_name=binding.name,
            arg_pos=i,
            suffix=suffix,
            ref='&' if ref else '',
        ))
        unpacked_bindings.append(Binding(
            name=binding.name + '_',
            ctype=binding.ctype,
            argument=binding.argument,
            default=binding.default,
        ))

    return body, unpacked_bindings

def get_base_name(f: NativeFunction) -> str:
    return f.func.name.name.base  # TODO: should be str(f.func.name.name)?

def get_view_info(fn: NativeFunctionWithDifferentiabilityInfo) -> Optional[str]:
    f = fn.func
    base_name = get_base_name(f)
    view_info = VIEW_FUNCTIONS.get(base_name, None)
    if view_info is None and base_name in RETURNS_VIEWS_OF_INPUT:
        view_info = "self"
    return view_info

# For view replay calls, we generate an ordinary Dispatcher::call() instead, because:
#  - We want to replay the entire call into the op, including any previously-set dispatch keys (including autograd!).
#  - The view replay call also is not part of the hot path.
def emit_view_call(f: NativeFunction, input_base: str, unpacked_args: Sequence[str]) -> str:
    # View replay functions use the standard Dispatcher::call API.
    if Variant.function in f.variants:
        call = CALL_DISPATCH_VIA_NAMESPACE.substitute(
            api_name=cpp.name(
                f.func,
                faithful_name_for_out_overloads=True,
            ),
            unpacked_args=unpacked_args)
    else:
        call = CALL_DISPATCH_VIA_METHOD.substitute(
            api_name=cpp.name(f.func),
            var=input_base,
            unpacked_method_args=unpacked_args[1:])
    return call

def emit_view_lambda(f: NativeFunction, unpacked_bindings: List[Binding]) -> str:
    """ Generate an additional lambda function to recover views in backward when as_strided is not supported.
    See Note [View + Inplace update for base tensor] and [View + Inplace update for view tensor] for more details."""
    input_base = 'input_base'
    replay_view_func = ''
    updated_unpacked_args: List[str] = []
    known_view_arg_simple_types: List[str] = ['int64_t', 'c10::optional<int64_t>', 'bool', 'IntArrayRef']
    for unpacked_binding in unpacked_bindings:
        arg, arg_type = unpacked_binding.name, unpacked_binding.type
        if arg == 'self_':
            updated_unpacked_args.append(input_base)
            continue
        if arg_type not in known_view_arg_simple_types:
            known_types_str = ', '.join(known_view_arg_simple_types)
            raise TypeError(f'You are adding an {arg_type} {arg} argument to op {cpp.name(f.func)} in addition to known types: '
                            f'{known_types_str}. Please update the list or materialize it so that it can be closed '
                            'over by value, also add a test in pytorch/xla/test/test_operations.py where this code '
                            'is exercised.')

        if arg_type == 'IntArrayRef':
            # It's not safe to close over IntArrayRef by value, since this is a
            # reference type, so materialize a vector to close over by value
            arg_vec = arg + '_vec'
            replay_view_func += ARRAYREF_TO_VEC.substitute(arg=arg, vec=arg_vec)
            updated_unpacked_args.append(arg_vec)
        elif arg_type == 'c10::optional<int64_t>':
            # Materialize int64_t? to int64_t
            arg_value = arg + '_val'
            replay_view_func += OPTIONAL_TO_VAL.substitute(arg=arg, val=arg_value, default='0')
            updated_unpacked_args.append(arg_value)
        else:
            updated_unpacked_args.append(arg)

    replay_view_call = emit_view_call(f, input_base, updated_unpacked_args)
    replay_view_func += REPLAY_VIEW_LAMBDA_FUNC.substitute(
        input_base=input_base,
        replay_view_call=replay_view_call)

    is_view_with_metadata_change = 'true' if cpp.name(f.func) in VIEW_FUNCTIONS_WITH_METADATA_CHANGE else 'false'

    return SETUP_REPLAY_VIEW_IF_NOT_SUPPORT_AS_STRIDED_OR_VIEW_WITH_METADATA_CHANGE.substitute(
        is_view_with_metadata_change=is_view_with_metadata_change,
        replay_view_func=replay_view_func)

def emit_view_body(fn: NativeFunctionWithDifferentiabilityInfo, var: str) -> Tuple[str, str]:
    # See NOTE [ Autograd View Variables ] in variable.h for details.
    f = fn.func
    base_name = get_base_name(f)
    view_info = get_view_info(fn)
    call = ''
    differentiable_outputs = gen_differentiable_outputs(fn)
    differentiable_output_vars = {r.name for r in differentiable_outputs}
    if not isinstance(view_info, str):
        raise TypeError(f'The view info should be a string for {base_name}, but it is: {view_info}')
    if len(differentiable_output_vars) == 0:
        # no output is differentiable (.indices() for SparseTensors for example)
        rhs_value = f'as_view({view_info}, {var}, /* is_bw_differentiable */ false, /* is_fw_differentiable */ false)'
    elif len(differentiable_output_vars) == 1:
        # Single differentiable output (Tensor or Tensor[])
        return_info = differentiable_outputs[0]
        # We only support simple Tensor or a TensorList for functions that return views
        if not is_tensor_type(return_info.type) and not is_tensor_list_type(return_info.type):
            raise RuntimeError(f'{base_name} that return differentiable views can only return Tensor or Tensor[]')
        # Only allow rebasing of the history if we return a single Tensor
        # If we are in a no grad block, raise a warning
        # See NOTE [ View + Inplace detection ] for more details about this logic
        if is_tensor_list_type(return_info.type):
            if base_name in MULTI_OUTPUT_SAFE_FUNCTIONS:
                creation_meta = 'CreationMeta::MULTI_OUTPUT_SAFE'
            else:
                creation_meta = 'CreationMeta::MULTI_OUTPUT_NODE'
            call += (f'as_view(/* base */ {view_info}, /* output */ {var}, /* is_bw_differentiable */ true, '
                     '/* is_fw_differentiable */ true, '
                     f'/* creation_meta */ {creation_meta});')
            rhs_value = f'std::move({var})'
        else:
            _, unpacked_bindings = unpack_args(f)
            call += emit_view_lambda(f, unpacked_bindings)
            creation_meta = 'GradMode::is_enabled() ? CreationMeta::DEFAULT: CreationMeta::NO_GRAD_MODE'
            rhs_value = (f'as_view(/* base */ {view_info}, /* output */ {var}, /* is_bw_differentiable */ true, '
                         '/* is_fw_differentiable */ true, '
                         f'/* view_func */ func, /* creation_meta */ {creation_meta})')
    else:
        # This could be supported but we don't need it at the moment, so keeping things simple.
        raise RuntimeError('Function that return multiple differentiable output '
                           'when at least one of them is view is not supported.')
    return call, rhs_value
