from pypy.module.cpyext.test.test_api import BaseApiTest
from pypy.module.cpyext.test.test_cpyext import AppTestCpythonExtensionBase

class TestListObject(BaseApiTest):
    def test_list(self, space, api):
        L = space.appexec([], """():
            class L(list):
                pass
            return L
        """)

        l = api.PyList_New(0)
        assert api.PyList_Check(l)
        assert api.PyList_CheckExact(l)

        l = space.call_function(L)
        assert api.PyList_Check(l)
        assert not api.PyList_CheckExact(l)

        assert not api.PyList_Check(space.newtuple([]))
        assert not api.PyList_CheckExact(space.newtuple([]))
    
    def test_get_size(self, space, api):
        l = api.PyList_New(0)
        assert api.PyList_GET_SIZE(l) == 0
        api.PyList_Append(l, space.wrap(3))
        assert api.PyList_GET_SIZE(l) == 1
    
    def test_size(self, space, api):
        l = space.newlist([space.w_None, space.w_None])
        assert api.PyList_Size(l) == 2
        assert api.PyList_Size(space.w_None) == -1
        assert api.PyErr_Occurred() is space.w_TypeError
        api.PyErr_Clear()

class AppTestListObject(AppTestCpythonExtensionBase):
    def test_listobject(self):
        import sys
        module = self.import_extension('foo', [
            ("newlist", "METH_NOARGS",
             """
             PyObject *lst = PyList_New(3);
             PyList_SetItem(lst, 0, PyInt_FromLong(3));
             PyList_SetItem(lst, 2, PyInt_FromLong(1000));
             PyList_SetItem(lst, 1, PyInt_FromLong(-5));
             return lst;
             """
             ),
            ("setlistitem", "METH_VARARGS",
             """
             PyObject *l = PyTuple_GetItem(args, 0);
             int index = PyInt_AsLong(PyTuple_GetItem(args, 1));
             Py_INCREF(Py_None);
             if (PyList_SetItem(l, index, Py_None) < 0)
                return NULL;
             Py_INCREF(Py_None);
             return Py_None;
             """
             ),
             ("appendlist", "METH_VARARGS",
             """
             PyObject *l = PyTuple_GetItem(args, 0);
             PyList_Append(l, PyTuple_GetItem(args, 1));
             Py_RETURN_NONE;
             """
             ),
            ])
        l = module.newlist()
        assert l == [3, -5, 1000]
        module.setlistitem(l, 0)
        assert l[0] is None

        class L(list):
            def __setitem__(self):
                self.append("XYZ")

        l = L([1])
        module.setlistitem(l, 0)
        assert len(l) == 1
        
        raises(SystemError, module.setlistitem, (1, 2, 3), 0)
    
        l = []
        module.appendlist(l, 14)
        assert len(l) == 1
        assert l[0] == 14

