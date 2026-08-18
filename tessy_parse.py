import xml.etree.ElementTree as ET
import sys
import re
import yaml
from itertools import product
from tessy_excel import *
from tessy_create_testlist import *
from pathlib import Path
import os

BUILD_FLAG = hasattr(sys, "_MEIPASS")  # True only inside the compiled exe

if BUILD_FLAG:
    ROOT = Path(sys.executable).resolve().parent   # runtime: input/, generated/
    RES_ROOT = Path(sys._MEIPASS)                   # bundled: template.xlsx
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT = SCRIPT_DIR
    RES_ROOT = ROOT


def expandArrayName(name):
    sizes = [int(s) for s in re.findall(r'\[(\d+)\]', name)]
    if not sizes:
        return [name]
    parts = re.split(r'\[\d+\]', name)
    expanded = []
    for indices in product(*[range(size) for size in sizes]):
        pieces = []
        for i, part in enumerate(parts):
            if i < len(indices):
                pieces.append(f'{part}[{indices[i]}]')
            else:
                # The split leaves a trailing part after the last '[n]' — no index for it.
                pieces.append(part)
        expanded.append(''.join(pieces))
    return expanded


def expandArrayRanges(value, count):
    ranges = str(value).split('|')
    if len(ranges) == count:
        return ranges
    return [value] * count


def expandSignFormat(fmt, length):
    # [zb] the value xml carries a collapsed sign ('unsigned'/'signed'); turn it into the full (e.g. unsigned char, unsigned short)
    suffix = {'8': 'char', '16': 'short', '32': 'long', '64': 'long long'}.get(str(length))
    if suffix and fmt in ('unsigned', 'signed'):
        return f'{fmt} {suffix}'
    return fmt


def applyNullPointerRows(testCases, variableNames, variableValidRanges, testClassifications):
    for pointerIndex, _ in enumerate(variableNames):
        if testClassifications[pointerIndex] != "pointer":
            continue
        targetName = str(variableValidRanges[pointerIndex]).strip()
        if not targetName.startswith("tessy_"):
            continue
        targetIndexes = []
        for i, target in enumerate(variableNames):
            if target == targetName or target.startswith(targetName + ".") or target.startswith(targetName + "["):
                targetIndexes.append(i)
        for row in testCases:
            if any(str(row[i]).strip().upper() == "NULL" for i in targetIndexes):
                row[pointerIndex] = "NULL"
                for i in targetIndexes:
                    row[i] = 0


### define
##fileName = "C:\\Users\\bktan2\\Desktop\\tessy_smart_tools\\myXml.xml"



def list_files_recursive( path , output_tessy_path , list_of_files ):
    for entry in os.listdir(path):
        value_path = os.path.join(path, entry)
        if os.path.isdir(value_path):
            # [zb] each category subfolder (e.g. PDM_CANCtrl_1.c) gets its own output
            # subfolder; use a local var so sibling directories don't accumulate paths.
            sub_output_path = os.path.join(output_tessy_path, entry)
            try:
                os.mkdir(sub_output_path)
            except Exception as e:
                pass
            list_files_recursive( value_path , sub_output_path , list_of_files )
        else:
            list_of_files.append(value_path)
            if entry[:5] == 'value':
                # [zb] output excel is named after the function (resolved inside Decode_Functions)
                # rather than the full value_*.xml entry name.
                Decode_Functions(value_path , path , output_tessy_path)
    

def mainBody():
    input_path = os.path.join(ROOT, 'input')
    config = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
    for project in config['projects']:
        for module in project['modules']:
            module_input = os.path.join(input_path, module['module_name'])
            if not os.path.isdir(module_input):
                continue
            output_tessy_path = module['excel_input_dir']
            os.makedirs(output_tessy_path, exist_ok=True)
            list_files_recursive(module_input, output_tessy_path, [])


### https://www.geeksforgeeks.org/python/reading-and-writing-xml-files-in-python/
### https://stackoverflow.com/questions/57351894/how-to-read-xml-file-using-python
def Decode_Functions(inputFile , stubDir , outputDir , row_aligned=False ):
    tree = ET.parse(inputFile)
    func = tree.getroot()
    if func.tag == "function":
        print("=============================================================")
        func_attributes = func.attrib
        try:
            func_name = func.attrib['name']
        except Exception as e:
            func_name = ""
        
        outputFile = os.path.join(outputDir, func_name + '.xlsx')
        try:
            func_testmethod = func.attrib['testmethod']
        except Exception as e:
            func_testmethod = ""
        variableName_list = []
        variableLength_list = []
        variableFormat_list = []
        variableDirection_list = []
        variableValidRange_list = []
        testClassification_list = []
        pointer_bases_seen = set()   # [zb] struct bases already emitted as a pointer (Error, Stat, ...)
        pointer_members_seen = set() # [zb] member-pointer tessy vars already emitted (StatePtr, ...)
        # [zb] a struct base is a *whole-struct* pointer only if every member of it is a pointer;
        # if only some members are pointers we emit a var for that specific member instead.
        whole_struct = {}
        for arg in func.findall('argument'):
            b = arg.attrib['name'].split('.')[0]
            whole_struct[b] = whole_struct.get(b, True) and arg.attrib.get('pointer') == "true"
        for arg in func.findall('argument'):
            a = arg.attrib
            if not all(k in a for k in ('name', 'len', 'format', 'direction', 'pointer')):
                print("function's ", func, " contain arguments missing name/len/format/direction attribute")
                break
            names = expandArrayName(a['name'])               ### [zb] expand array names to one entry per index
            fmt = expandSignFormat(a['format'], a['len'])
            tessy_names = ["tessy_" + n for n in names]

            if a['pointer'] == "true" and whole_struct[a['name'].split('.')[0]]:
                for n, tn in zip(names, tessy_names):
                    base = n.split('.')[0]           ### [zb] strip struct member
                    if base not in pointer_bases_seen:
                        pointer_bases_seen.add(base)
                        variableName_list.append(base)
                        variableLength_list.append(a['len'])
                        variableFormat_list.append(fmt)
                        variableDirection_list.append(a['direction'])
                        if a.get('range') == "NULL":
                            variableValidRange_list.append("NULL")
                        else:
                            variableValidRange_list.append("tessy_" + base)
                        testClassification_list.append("pointer")

                    variableName_list.append(tn)
                    variableLength_list.append(a['len'])
                    variableFormat_list.append(fmt)
                    variableDirection_list.append("inout")
                    variableValidRange_list.append(a.get('range', '0~1'))
                    testClassification_list.append(a.get('testmethod', 'decision'))
            elif a['pointer'] == "true":
                # [zb] only this member of the struct is a pointer -> emit a var for the specific member,
                # not the whole struct. the pointer target is a scalar sized by len (unsigned char/long/...),
                # NOT the enclosing struct type. array base (lol[N].abc) -> tessy_abc_array[N], one element per member.
                member = re.sub(r'\[\d+\]', '', a['name'].split('.')[-1])
                target = expandSignFormat('unsigned', a['len'])
                is_array = len(names) > 1
                tessy_var = "tessy_" + member + ("_array" if is_array else "")
                # [zb] one tessy element per struct member; expandArrayName gives tessy_abc_array[0..N-1]
                # (the type [N] is shown only on [0], see convertDirectionToTessyFormat dedup).
                tessy_cols = expandArrayName(tessy_var + "[%d]" % len(names)) if is_array else [tessy_var]
                for n, tc in zip(names, tessy_cols):
                    variableName_list.append(n)
                    variableLength_list.append(a['len'])
                    variableFormat_list.append(fmt)
                    variableDirection_list.append(a['direction'])
                    if a.get('range') == "NULL":
                        variableValidRange_list.append("NULL")
                    else:
                        variableValidRange_list.append(tc)
                    testClassification_list.append("pointer")
                if tessy_var not in pointer_members_seen:
                    pointer_members_seen.add(tessy_var)
                    tessy_fmt = target + " [%d]" % len(names) if is_array else target
                    for tc in tessy_cols:
                        variableName_list.append(tc)
                        variableLength_list.append(a['len'])
                        variableFormat_list.append(tessy_fmt)
                        variableDirection_list.append("inout")
                        variableValidRange_list.append(a.get('range', '0~1'))
                        testClassification_list.append(a.get('testmethod', 'decision'))
            else:
                variableName_list += names
                variableLength_list += [a['len']] * len(names)
                variableFormat_list += [fmt] * len(names)
                variableDirection_list += [a['direction']] * len(names)
                variableValidRange_list += expandArrayRanges(a.get('range', '0~1'), len(names))
                testClassification_list += [a.get('testmethod', 'decision')] * len(names)


        # [zb] an inout variable appears twice - once as input, once as output
        inputs = [i for i, d in enumerate(variableDirection_list) if d in ("in", "inout")]
        outputs = [i for i, d in enumerate(variableDirection_list) if d in ("out", "inout")]
        order = inputs + outputs
        variableDirection_list = ["in"] * len(inputs) + ["out"] * len(outputs)


        # [zb] reorder
        variableName_list = [variableName_list[i] for i in order]
        variableLength_list = [variableLength_list[i] for i in order]
        variableFormat_list = [variableFormat_list[i] for i in order]
        variableValidRange_list = [variableValidRange_list[i] for i in order]
        testClassification_list = [testClassification_list[i] for i in order]
        stub_list = Decode_Stubs(stubDir)
        print("creating Stub for",func_name,"() with total stub count ",len(stub_list))
        print("=============================================================")
        for i in range(len(stub_list)):
            print("    ", stub_list[i][0])
        print("=============================================================")
        if row_aligned:
            testCase_list , testMethod_list = createRowAlignedTestList(func_name , variableName_list, variableLength_list, variableFormat_list, variableDirection_list , variableValidRange_list ,testClassification_list)
        else:
            testCase_list , testMethod_list = createTestListforFunction(func_name , variableName_list, variableLength_list, variableFormat_list, variableDirection_list , variableValidRange_list ,testClassification_list)
        applyNullPointerRows(testCase_list, variableName_list, variableValidRange_list, testClassification_list)
        updateTestlistToExcel(func_name , os.path.join(RES_ROOT, 'template.xlsx'), outputFile ,  testCase_list , testMethod_list , stub_list ,  variableName_list , variableDirection_list , variableFormat_list)
        print("testCase_list:")
        for i in range(len(testCase_list)):
            print("    Test Cases (",i,") input : ", testCase_list[i] , "; method : " , testMethod_list[i])


def Decode_Stubs(stubDir):
    # [zb] collect every called function across all stub_*.xml in the folder (file-level), deduped by name
    stub_list = []
    seen = set()
    for fname in sorted(os.listdir(stubDir)):
        if not (fname.startswith('stub') and fname.endswith('.xml')):
            continue
        try:
            for fn in ET.parse(os.path.join(stubDir, fname)).getroot().iter('called_functions'):
                for func in fn.iter('function'):
                    if func.attrib['name'] in seen:
                        continue
                    seen.add(func.attrib['name'])
                    ret = func.find('return')
                    stub_detail = [func.attrib['name'],
                                   ret.findtext('type', 'void') if ret is not None else 'void',
                                   ret.findtext('name', 'N/A') if ret is not None else 'N/A']
                    args = []
                    for arg in func.findall('arguments/argument'):
                        args += [arg.findtext('type'), arg.findtext('name')]
                    stub_detail.append(args)
                    stub_list.append(stub_detail)
        except Exception as X:
            print("Stub format is not correct for file" , fname)
    return stub_list


if __name__ == '__main__':
    args = sys.argv
    # args[0] = current file
    # args[1] = function name
    # args[2:] = function args : (*unpacked)
    mainBody()
        
