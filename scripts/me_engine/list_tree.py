# encoding:utf-8
from __future__ import print_function
import sys
print("HELLO FROM SCRIPT", sys.argv)
p = projects.open(sys.argv[1])
print("opened:", p.path)
def walk(o, d=0):
    for c in o.get_children(False):
        print("  " * d + c.get_name() + "  " + str(c.type))
        walk(c, d + 1)
walk(p)
p.close()
print("closed")
