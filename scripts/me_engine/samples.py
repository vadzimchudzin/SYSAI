# encoding:utf-8
from __future__ import print_function
import os
path = r"C:\p\samples.project"
if os.path.exists(path): os.remove(path)
p = projects.create(path, True)
d = p.create_dut("T_Mode", DutType.Enumeration)
print("dut", d.textual_declaration.text)
s = p.create_dut("T_Rec", DutType.Structure)
s.textual_declaration.replace("TYPE T_Rec :\nSTRUCT\n\ta : INT;\n\tb : REAL;\nEND_STRUCT\nEND_TYPE\n")
u = p.create_dut("T_U", DutType.Union)
al = p.create_dut("T_A", DutType.Alias, "INT")
fb = p.create_pou("FB_Motor", PouType.FunctionBlock)
fb.textual_declaration.replace("FUNCTION_BLOCK FB_Motor\nVAR_INPUT\n\tStart : BOOL;\nEND_VAR\nVAR_OUTPUT\n\tRun : BOOL;\nEND_VAR\n")
fb.textual_implementation.replace("Run := Start;\n")
m = fb.create_method("Reset", "BOOL")
m.textual_implementation.replace("Reset := TRUE;\n")
a = fb.create_action("ActStop")
a.textual_implementation.replace("Run := FALSE;\n")
pr = fb.create_property("Speed", "INT")
fn = p.create_pou("F_Add", PouType.Function, return_type="INT")
fn.textual_declaration.replace("FUNCTION F_Add : INT\nVAR_INPUT\n\ta, b : INT;\nEND_VAR\n")
fn.textual_implementation.replace("F_Add := a + b;\n")
f = p.create_folder("Folder1")
g = p.create_gvl("GVL_In")
g.textual_declaration.replace("{attribute 'qualified_only'}\nVAR_GLOBAL\n\tx : BOOL;\nEND_VAR\n")
try:
    g.move(f)
    print("moved")
except Exception as e:
    print("move failed", e)
p.save()
for c in p.get_children(True):
    print("obj", c.get_name(), c.type)
p.close()
