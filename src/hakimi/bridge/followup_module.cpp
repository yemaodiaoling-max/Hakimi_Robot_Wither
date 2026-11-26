#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <cstddef>

extern "C" int hakimi_followup_relation(
    const char*, std::size_t, const char*, std::size_t) noexcept;

static PyObject* is_continuation(PyObject*, PyObject* args) {
    PyObject* previous;
    PyObject* incoming;
    if (!PyArg_ParseTuple(args, "UU", &previous, &incoming)) return nullptr;
    if (PyUnicode_GET_LENGTH(previous) > 16384 || PyUnicode_GET_LENGTH(incoming) > 16384)
        Py_RETURN_FALSE;
    Py_ssize_t previous_size;
    Py_ssize_t incoming_size;
    const char* first = PyUnicode_AsUTF8AndSize(previous, &previous_size);
    if (!first) return nullptr;
    const char* next = PyUnicode_AsUTF8AndSize(incoming, &incoming_size);
    if (!next) return nullptr;
    return PyBool_FromLong(hakimi_followup_relation(first, previous_size, next, incoming_size));
}

static PyMethodDef methods[] = {
    {"is_continuation", is_continuation, METH_VARARGS, "Check whether a new message qualifies the previous request."},
    {nullptr, nullptr, 0, nullptr}
};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_followup_rules", nullptr, -1, methods};

PyMODINIT_FUNC PyInit__followup_rules() {
    return PyModule_Create(&module);
}
