'''Various useful routines maybe not appropriate elsewhere'''

import numpy
import os
import scipy.sparse
import sys
import subprocess
import types
from functools import  reduce


def get_git_revision_hash():
    """ Return git revision.

    Adapted from:
        http://stackoverflow.com/questions/14989858/get-the-current-git-hash-in-a-python-script

    Returns
    -------
    sha1 : string
        git hash with -dirty appended if uncommitted changes.
    """

    try:
        srcs = [s for s in sys.path if 'pauxy' in s]
        if len(srcs) > 1:
            for s in srcs:
                if 'setup.py' in os.listdir(s):
                    src = s
                    break
                else:
                    src = srcs[0]
        else:
            src = srcs[0]


        sha1 = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                       cwd=src).strip()
        suffix = subprocess.check_output(['git', 'status',
                                          '--porcelain',
                                          './pauxy'],
                                         cwd=src).strip()
    except:
        suffix = False
        sha1 = 'none'.encode()
    if suffix:
        return sha1.decode('utf-8') + '-dirty'
    else:
        return sha1.decode('utf-8')


def is_h5file(obj):
    t = str(type(obj))
    cond = 'h5py' in t
    return cond


def is_class(obj):
    cond = (hasattr(obj, '__class__') and (('__dict__') in dir(obj)
            and not isinstance(obj, types.FunctionType)
            and not is_h5file(obj)))

    return cond


def serialise(obj, verbose=0):

    obj_dict = {}
    if isinstance(obj, dict):
        items = obj.items()
    else:
        items = obj.__dict__.items()

    for k, v in items:
        if isinstance(v, scipy.sparse.csr_matrix):
            pass
        elif isinstance(v, scipy.sparse.csc_matrix):
            pass
        elif is_class(v):
            # Object
            obj_dict[k] = serialise(v, verbose)
        elif isinstance(v, dict):
            obj_dict[k] = serialise(v)
        elif isinstance(v, types.FunctionType):
            # function
            if verbose == 1:
                obj_dict[k] = str(v)
        elif hasattr(v, '__self__'):
            # unbound function
            if verbose == 1:
                obj_dict[k] = str(v)
        elif k == 'estimates' or k == 'global_estimates':
            pass
        elif k == 'walkers':
            obj_dict[k] = [str(x) for x in v][0]
        elif isinstance(v, numpy.ndarray):
            if verbose == 3:
                if v.dtype == complex:
                    obj_dict[k] = [v.real.tolist(), v.imag.tolist()]
                else:
                    obj_dict[k] = v.tolist(),
            elif verbose == 2:
                if len(v.shape) == 1:
                    if v.dtype == complex:
                        obj_dict[k] = [[v.real.tolist(), v.imag.tolist()]]
                    else:
                        obj_dict[k] = v.tolist(),
        elif k == 'store':
            if verbose == 1:
                obj_dict[k] = str(v)
        elif isinstance(v, (int, float, bool, str)):
            obj_dict[k] = v
        elif isinstance(v, complex):
            obj_dict[k] = v.real
        elif v is None:
            obj_dict[k] = v
        elif is_h5file(v):
            if verbose == 1:
                obj_dict[k] = v.filename
        else:
            pass

    return obj_dict

class dotdict(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

def update_stack(stack_size, num_slices, name="stack", verbose=False):
    lower_bound = min(stack_size, num_slices)
    upper_bound = min(stack_size, num_slices)

    while (num_slices//lower_bound) * lower_bound < num_slices:
        lower_bound -= 1
    while (num_slices//upper_bound) * upper_bound < num_slices:
        upper_bound += 1

    if (stack_size-lower_bound) <= (upper_bound - stack_size):
        stack_size = lower_bound
    else:
        stack_size = upper_bound
    if verbose:
        print("# Initial {} upper_bound is {}".format(name, upper_bound))
        print("# Initial {} lower_bound is {}".format(name, lower_bound))
        print("# Adjusted {} size is {}".format(name, stack_size))
    return stack_size

def print_section_header(string):
    header = """
    ################################################
    #                                              #
    #                                              #
    #                                              #
    """
    box_len = len("################################################")
    str_len = len(string)
    start = box_len // 2 - str_len // 2 - 1
    init = "#" + ' '*start
    end = box_len - (box_len//2 + str_len // 2) - 1
    fin = ' '*end + "#"
    footer = """
    #                                              #
    #                                              #
    #                                              #
    ################################################
    """
    print(header + init + string + fin + footer)

def merge_dicts(a, b, path=None):
    if path is None: path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge_dicts(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass # same leaf value
            else:
                raise Exception('Conflict at %s' % '.'.join(path + [str(key)]))
        else:
            a[key] = b[key]
    return a

def get_from_dict(d, k):
    """Get value from nested dictionary.

    Taken from:
        https://stackoverflow.com/questions/28225552/is-there-a-recursive-version-of-the-dict-get-built-in

    Parameters
    ----------
    d : dict
    k : list
        List specifying key to extract.

    Returns
    -------
    value : Return type or None.
    """
    try:
        return reduce(dict.get, k, d)
    except TypeError:
        # Value not found.
        return None

def get_numeric_names(d):
    names = []
    size = 0
    for k, v in d.items():
        if isinstance(v, (numpy.ndarray)):
            names.append(k)
            size += v.size
        elif isinstance(v, (float, complex, int)):
            names.append(k)
            size += 1
    return names, size
