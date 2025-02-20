import os
import logging

import numpy as np

from .context import (
    XBuffer,
    XContext,
    ModuleNotAvailable,
    available,
    classes_from_kernels,
    sort_classes,
    sources_from_classes,
    _concatenate_sources,
)

from .specialize_source import specialize_source

log = logging.getLogger(__name__)

try:
    import cupy
    from cupyx.scipy import fftpack as cufftp

    _enabled = True
except ImportError:
    log.info("cupy is not installed, ContextCupy will not be available")
    cupy = ModuleNotAvailable(
        message=("cupy is not installed. " "ContextCupy is not available!")
    )
    cufftp = cupy
    _enabled = False


cudaheader = [
    """\
typedef signed long long int64_t; //only_for_context cuda
typedef signed char      int8_t;  //only_for_context cuda
"""
]


def nplike_to_cupy(arr):
    return cupy.array(arr)


class ContextCupy(XContext):

    """
    Creates a Cupy Context object, that allows performing the computations
    on nVidia GPUs.

    To select device use cupy.Device(<n>).use()

    Args:
        default_block_size (int):  CUDA thread size that is used by default
            for kernel execution in case a block size is not specified
            directly in the kernel object. The default value is 256.
    Returns:
        ContextCupy: context object.

    """

    def __init__(self, default_block_size=256, device=None):

        if device is not None:
            cupy.Device(device).use()

        super().__init__()

        self.default_block_size = default_block_size

    def _make_buffer(self, capacity):
        return BufferCupy(capacity=capacity, context=self)

    def add_kernels(
        self,
        sources,
        kernels,
        specialize=True,
        save_source_as=None,
        extra_cdef=None,
        extra_classes=[],
        extra_headers=[],
    ):

        """
        Adds user-defined kernels to to the context. The kernel source
        code is provided as a string and/or in source files and must contain
        the kernel names defined in the kernel descriptions.
        Args:
            sources (list): List of source codes that are concatenated before
                compilation. The list can contain strings (raw source code),
                File objects and Path objects.
            kernels (dict): Dictionary with the kernel descriptions
                in the form given by the following examples. The descriptions
                define the kernel names, the type and name of the arguments
                and identify one input argument that defines the number of
                threads to be launched (only on cuda/opencl).
            specialize_code (bool): If True, the code is specialized using
                annotations in the source code. Default is ``True``
            save_source_as (str): Filename for saving the specialized source
                code. Default is ```None```.
        Example:

        .. code-block:: python

            # A simple kernel
            src_code = '''
            /*gpukern*/
            void my_mul(const int n,
                /*gpuglmem*/ const double* x1,
                /*gpuglmem*/ const double* x2,
                /*gpuglmem*/       double* y) {
                int tid = 0 //vectorize_over tid
                y[tid] = x1[tid] * x2[tid];
                //end_vectorize
                }
            '''

            # Prepare description
            kernel_descriptions = {
                "my_mul": xo.Kernel(
                    args=[
                        xo.Arg(xo.Int32, name="n"),
                        xo.Arg(xo.Float64, pointer=True, const=True, name="x1"),
                        xo.Arg(xo.Float64, pointer=True, const=True, name="x2"),
                        xo.Arg(xo.Float64, pointer=True, const=False, name="y"),
                    ],
                    n_threads="n",
                    ),
            }

            # Import kernel in context
            ctx.add_kernels(
                sources=[src_code],
                kernels=kernel_descriptions,
                save_source_as=None,
            )

            # With a1, a2, b being arrays on the context, the kernel
            # can be called as follows:
            ctx.kernels.my_mul(n=len(a1), x1=a1, x2=a2, y=b)
        """

        classes = classes_from_kernels(kernels)
        classes.update(extra_classes)
        classes = sort_classes(classes)
        cls_sources = sources_from_classes(classes)

        headers = cudaheader + extra_headers

        sources = headers + cls_sources + sources

        source, folders = _concatenate_sources(sources)
        source = "\n".join(['extern "C"{', source, "}"])

        if specialize:
            # included files are searched in the same folders od the src_filed
            source = specialize_source(
                source, specialize_for="cuda", search_in_folders=folders
            )

        if save_source_as is not None:
            with open(save_source_as, "w") as fid:
                fid.write(source)

        module = cupy.RawModule(code=source)

        for pyname, kernel in kernels.items():
            if kernel.c_name is None:
                kernel.c_name = pyname

            self.kernels[pyname] = KernelCupy(
                function=module.get_function(kernel.c_name),
                description=kernel,
                block_size=self.default_block_size,
                context=self,
            )

    def nparray_to_context_array(self, arr):
        """
        Copies a numpy array to the device memory.

        Args:
            arr (numpy.ndarray): Array to be transferred

        Returns:
            cupy.ndarray:The same array copied to the device.

        """
        dev_arr = cupy.array(arr)
        return dev_arr

    def nparray_from_context_array(self, dev_arr):
        """
        Copies an array to the device to a numpy array.

        Args:
            dev_arr (cupy.ndarray): Array to be transferred.
        Returns:
            numpy.ndarray: The same data copied to a numpy array.

        """
        return dev_arr.get()

    @property
    def nplike_lib(self):
        """
        Module containing all the numpy features supported by cupy.
        """
        return cupy

    def synchronize(self):
        """
        Ensures that all computations submitted to the context are completed.
        Equivalent to ``cupy.cuda.stream.get_current_stream().synchronize()``
        """
        cupy.cuda.stream.get_current_stream().synchronize()

    def zeros(self, *args, **kwargs):
        """
        Allocates an array of zeros on the device. The function has the same
        interface of numpy.zeros"""
        return self.nplike_lib.zeros(*args, **kwargs)

    def plan_FFT(
        self,
        data,
        axes,
    ):
        """
        Generates an FFT plan object to be executed on the context.

        Args:
            data (cupy.ndarray): Array having type and shape for which the FFT
                needs to be planned.
            axes (sequence of ints): Axes along which the FFT needs to be
                performed.
        Returns:
            FFTCupy: FFT plan for the required array shape, type and axes.

        Example:

        .. code-block:: python

            plan = context.plan_FFT(data, axes=(0,1))

            data2 = 2*data

            # Forward tranform (in place)
            plan.transform(data2)

            # Inverse tranform (in place)
            plan.itransform(data2)
        """
        return FFTCupy(self, data, axes)

    @property
    def kernels(self):

        """
        Dictionary containing all the kernels that have been imported to the context.
        The syntax ``context.kernels.mykernel`` can also be used.
        """

        return self._kernels


class BufferCupy(XBuffer):
    def _make_context(self):
        return ContextCupy()

    def _new_buffer(self, capacity):
        return cupy.zeros(shape=(capacity,), dtype=cupy.uint8)

    def update_from_native(self, offset, source, source_offset, nbytes):
        """Copy data from native buffer into self.buffer starting from offset"""
        self.buffer[offset : offset + nbytes] = source[
            source_offset : source_offset + nbytes
        ]

    def copy_native(self, offset, nbytes):
        """Return a new cupy buffer with data from offset"""
        return self.buffer[offset : offset + nbytes].copy()

    def copy_to_native(self, dest, dest_offset, source_offset, nbytes):
        """copy data from self to source from offset and nbytes"""
        dest[dest_offset : dest_offset + nbytes] = self.buffer[
            source_offset : source_offset + nbytes
        ]

    def update_from_buffer(self, offset, source):
        """Copy data from python buffer such as bytearray, bytes, memoryview, numpy array.data"""
        nbytes = len(source)
        self.buffer[offset : offset + nbytes] = cupy.array(
            np.frombuffer(source, dtype=np.uint8)
        )

    def to_nplike(self, offset, dtype, shape):
        """view in nplike"""
        nbytes = np.prod(shape) * dtype.itemsize
        return (
            self.buffer[offset : offset + nbytes]
            .view(dtype=dtype)
            .reshape(*shape)
        )

    def update_from_nplike(self, offset, dest_dtype, value):
        if dest_dtype != value.dtype:
            value = value.astype(dtype=dest_dtype)  # make a copy
        src = value.view("int8")
        self.buffer[offset : offset + src.nbytes] = value.view("int8")

    def to_bytearray(self, offset, nbytes):
        """copy in byte array: used in update_from_xbuffer"""
        return self.buffer[offset : offset + nbytes].get().tobytes()

    def to_pointer_arg(self, offset, nbytes):
        """return data that can be used as argument in kernel"""
        return self.buffer[offset : offset + nbytes]


class KernelCupy(object):
    def __init__(
        self,
        function,
        description,
        block_size,
        context,
    ):

        self.function = function
        self.description = description
        self.block_size = block_size
        self.context = context

    def to_function_arg(self, arg, value):
        if arg.pointer:
            if hasattr(arg.atype, "_dtype"):  # it is numerical scalar
                if hasattr(value, "dtype"):  # nparray
                    assert isinstance(value, cupy.ndarray)
                    return value.data
                elif hasattr(value, "_shape"):  # xobject array
                    raise NotImplementedError
            else:
                raise ValueError(
                    f"Invalid value {value} for argument {arg.name} "
                    f"of kernel {self.description.pyname}"
                )
        else:
            if hasattr(arg.atype, "_dtype"):  # it is numerical scalar
                return arg.atype(value)  # try to return a numpy scalar
            elif hasattr(arg.atype, "_size"):  # it is a compound xobject
                return value._buffer.buffer[value._offset :]
            else:
                raise ValueError(
                    f"Invalid value {value} for argument {arg.name} of kernel {self.description.pyname}"
                )

    @property
    def num_args(self):
        return len(self.description.args)

    def __call__(self, **kwargs):
        assert len(kwargs.keys()) == self.num_args
        arg_list = []
        for arg in self.description.args:
            vv = kwargs[arg.name]
            arg_list.append(self.to_function_arg(arg, vv))

        if isinstance(self.description.n_threads, str):
            n_threads = kwargs[self.description.n_threads]
        else:
            n_threads = self.description.n_threads

        grid_size = int(np.ceil(n_threads / self.block_size))
        self.function((grid_size,), (self.block_size,), arg_list)


class FFTCupy(object):
    def __init__(self, context, data, axes):

        self.context = context
        self.axes = axes

        assert len(data.shape) > max(axes)

        from cupyx.scipy import fftpack as cufftp

        if data.flags.f_contiguous:
            self._ax = [data.ndim - 1 - aa for aa in axes]
            _dat = data.T
            self.f_contiguous = True
        else:
            self._ax = axes
            _dat = data
            self.f_contiguous = False

        self._fftplan = cufftp.get_fft_plan(
            _dat, axes=self._ax, value_type="C2C"
        )

    def transform(self, data):
        if self.f_contiguous:
            _dat = data.T
        else:
            _dat = data
        _dat[:] = cufftp.fftn(_dat, axes=self._ax, plan=self._fftplan)[:]
        """The transform is done inplace"""

    def itransform(self, data):
        """The transform is done inplace"""
        if self.f_contiguous:
            _dat = data.T
        else:
            _dat = data
        _dat[:] = cufftp.ifftn(_dat, axes=self._ax, plan=self._fftplan)[:]


if _enabled:
    available.append(ContextCupy)
