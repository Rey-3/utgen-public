import xml.etree.ElementTree as ET
def createTestListforFunction(func_name , variableName_list, variableLength_list, variableFormat_list, variableDirection_list , variableValidRange_list , testCassification_list):
    print("creating Test cases for",func_name,"() with total arguments/variables",len(variableName_list))
    print("=============================================================")
    print("    with variables :",variableName_list);
    print("    with length :",variableLength_list);
    print("    with directions :",variableDirection_list);
    print("    with validRange :",variableValidRange_list);
    print("    with testClassification :",testCassification_list);
    print("=============================================================")
    ### create a single row test list first.
    ### row represent test cases , col represent test injection value for each arguments/variables
    col = len(variableName_list)
    injectionValue_list = []
    input_rows = output_rows = 0
    for arg in range(col):
        values = (_row_values(variableValidRange_list[arg]) if variableDirection_list[arg] == "out" and testCassification_list[arg] != "pointer" else
                  getRequiredTestValue(variableName_list[arg], variableLength_list[arg], variableFormat_list[arg],
                                       variableValidRange_list[arg], testCassification_list[arg]))
        injectionValue_list.append(values)
        if variableDirection_list[arg] == "out":
            output_rows = max(output_rows, len(values))
        else:
            input_rows = max(input_rows, len(values))
    ##print("argument List:" , injectionValue_list)
    ### each-choice coverage: every value of every variable appears, rows = the largest variable's value count (no cartesian blow-up)
    rows = input_rows or output_rows
    myTestList, testMethod = [], []
    for i in range(rows):
        # myTestList = []
        # testMethod = []
        # if len(injectionValue_list) > 0:
        #     for r in range(len(injectionValue_list)):
        #         myTestList , testMethod = updateTestList(myTestList , testMethod, variableValidRange_list[r] , injectionValue_list[r] , variableLength_list[r] , variableFormat_list[r])
        #         if r==5:
        #             break
        row = []
        for c, values in enumerate(injectionValue_list):
            if variableDirection_list[c] == "out" and testCassification_list[c] != "pointer":
                row.append(values[i] if i < len(values) else 0)
            else:
                row.append(values[i % len(values)] if values else 0)
        myTestList.append(row)
        testMethod.append([classifyTestMethod(variableLength_list[c], variableFormat_list[c], variableValidRange_list[c], injectionValue_list[c][i % len(injectionValue_list[c])]) if injectionValue_list[c] and variableDirection_list[c] != "out" else 'Ignore' for c in range(col)])
    return (myTestList, testMethod)


def createRowAlignedTestList(func_name, variableName_list, variableLength_list, variableFormat_list, variableDirection_list, variableValidRange_list, testCassification_list):
    """Replay the range verbatim as row-aligned test cases.
    """
    col = len(variableName_list)
    # parse every variable's row vector; output columns carry expected values
    parsed = []
    input_rows = output_rows = 0
    for i in range(col):
        rng = str(variableValidRange_list[i])
        toks = [t.strip() for t in rng.split(",")] if "," in rng else [rng.strip()]
        parsed.append(toks)
        if variableDirection_list[i] == "out":
            output_rows = max(output_rows, len(toks))
        else:
            input_rows = max(input_rows, len(toks))
    num_tc = input_rows or output_rows or 1

    myTestList, testMethod = [], []
    for r in range(num_tc):
        row, meth = [], []
        for i in range(col):
            toks = parsed[i]
            if testCassification_list[i] == "pointer":
                value = variableValidRange_list[i]        # tessy binding, constant across rows
            else:
                value = convertHexToDec(toks[r]) if r < len(toks) else (0 if variableDirection_list[i] == "out" else convertHexToDec(toks[-1]))
            row.append(value)
            if variableDirection_list[i] == "out":
                meth.append("Ignore")
            elif OutofRangeChecker(variableLength_list[i], variableFormat_list[i], value) == 1:
                meth.append("Invalid Test Case")
            else:
                meth.append("Functional Test")
        myTestList.append(row)
        testMethod.append(meth)
    return (myTestList, testMethod)


def _row_values(value):
    values = []
    for token in str(value).split(","):
        values.append(convertHexToDec(token))
    return values


def classifyTestMethod(variableLength, variableFormat, variableValidRange, value):
    if OutofRangeChecker(variableLength, variableFormat, value) == 1:
        return 'Invalid Test Case'
    if BoundaryChecker(variableValidRange, value) == 1:
        return 'Boundary Test'
    return 'Functional Test'

def getRequiredTestValue(variableName , variableLength , variableFormat, variableValidRange , testCassification ):
    endSearch = 0
    injectionValue_list = []
    #print("argument:",variableName,"is", testCassification)
    if testCassification == "math":
        if variableValidRange == "full range":
            print("full range not supported yet")
        else:
            range_min = int(convertHexToDec(variableValidRange[:variableValidRange.find("~")]))
            range_max = int(convertHexToDec(variableValidRange[variableValidRange.find("~")+1:]))
            range_mid = int((range_max + range_min)/2)
            injectionValue_list.append(range_min)
            if range_mid!=range_min and range_mid!=range_max:
                injectionValue_list.append(range_mid)
            injectionValue_list.append(range_max)
            injectionValue_list.append(range_min - 1)
            injectionValue_list.append(range_max + 1)
    elif testCassification == "decision":
        while True:
            if variableValidRange.find(",") < 0:
                endSearch = 1
                tmp_variableValidRange = variableValidRange
            else:
                tmp_variableValidRange = variableValidRange[:variableValidRange.find(",")]
            #print(tmp_variableValidRange)
            if tmp_variableValidRange.find("~") < 0:
                injectionValue_list.append(convertHexToDec(tmp_variableValidRange))
            else:
                range_min = int(convertHexToDec(tmp_variableValidRange[:tmp_variableValidRange.find("~")]))
                range_max = int(convertHexToDec(tmp_variableValidRange[tmp_variableValidRange.find("~")+1:]))
                for i in range(int(range_min),int(range_max)+1):
                    injectionValue_list.append(i)
            variableValidRange = variableValidRange[variableValidRange.find(",")+1:]
            if endSearch == 1:
                break
    elif testCassification == "pointer":
        # [zb] assign tessy_name to pointer value
        injectionValue_list.append(variableValidRange)


    new_injectionValue_list = []
    for i in range(len(injectionValue_list)):
        if OutofRangeChecker(variableLength , variableFormat , injectionValue_list[i]) == 0 and (testCassification == "decision" or DuplicateChecker(i , injectionValue_list) == 0) :
            new_injectionValue_list.append(injectionValue_list[i])
    return new_injectionValue_list

### DEPRECATED - NOT USED. Old cartesian builder; replaced by each-choice in createTestListforFunction.
def updateTestList(prev_testList , prev_testMethod , variableValidRange , injectionValue , variableLength , variableFormat):
    newTestList = []
    newTestMethod = []
    if len(prev_testList) == 0:
        ### First test case , only 1 variable
        ### so newTestList and newTestMethod update 1st value and 1st method.
        for i in range(len(injectionValue)):
            newTestList.append([injectionValue[i]])
            if OutofRangeChecker(variableLength, variableFormat, injectionValue[i]) == 1:
                newTestMethod.append(['Invalid Test Case'])
            elif BoundaryChecker(variableValidRange , injectionValue[i]) == 1:
                newTestMethod.append(['Boundary Test'])
            else:
                newTestMethod.append(['Functional Test'])
    else:
        ### Subsequence Test case
        ### if the injectionValue is not empty, then...
        if len(injectionValue)!=0:
            for j in range(len(prev_testList)):
                ### for each of the previous test list, testCase and testMethod update according to given test method.
                for z in range(len(injectionValue)):
                    temp2 = []
                    temp2 = prev_testList[j].copy()
                    temp2.append(injectionValue[z])
                    newTestList.append(temp2)
                    temp3 = []
                    temp3 = prev_testMethod[j].copy()
                    if OutofRangeChecker(variableLength, variableFormat , injectionValue[z]) == 1:
                        temp3.append('Invalid Test Case')
                    elif BoundaryChecker(variableValidRange , injectionValue[z]) == 1:
                        temp3.append('Boundary Test')
                    else:
                        temp3.append('Functional Test')
                    newTestMethod.append(temp3)
        ### if the injectionValue is empty (normally is output type), just append with blank value and method
        else:
            for j in range(len(prev_testList)):
                tmp = []
                tmp = prev_testList[j]
                tmp.append(0)
                tmp2 = []
                tmp2 = prev_testMethod[j]
                tmp2.append("Ignore")
                newTestList.append(tmp)
                newTestMethod.append(tmp2)
    return (newTestList,newTestMethod)

def convertHexToDec(value):
    value = value.strip()
    if value.find("0x") == 0:
        return int(value[2:],16)
    if value.find("0b") == 0:
        return int(value[2:],2)
    try:
        return int(value)          # plain decimal incl. negative -> int
    except ValueError:
        return value               # named/enum value (e.g. ABC) -> keep as string
    
def OutofRangeChecker(variableLength , variableFormat , value):
    outOfRangeFlg = 0
    try:
        if variableFormat.startswith("unsigned"):
            maximum = pow(2,int(variableLength)) - 1
            if int(value) > maximum:
                outOfRangeFlg = 1
        else:
            minimum = -pow(2,int(variableLength)-1)
            maximum = pow(2,int(variableLength)-1)
            maximum = maximum - 1
            if int(value) > maximum or int(value) < minimum:
                outOfRangeFlg = 1
    except ValueError:
        {}
    return outOfRangeFlg

    
def BoundaryChecker(variableValidRange , value):
    boundaryFlg = 0
    endSearch = 0
    try:
        while True:
            if variableValidRange.find(",") < 0:
                endSearch = 1
                tmp_variableValidRange = variableValidRange
            else:
                tmp_variableValidRange = variableValidRange[:variableValidRange.find(",")]
            #print(tmp_variableValidRange)
            if tmp_variableValidRange.find("~") < 0:
                cmpValue = convertHexToDec(tmp_variableValidRange)
                if value == int(cmpValue)-1 :
                    boundaryFlg = 1
                elif value == int(cmpValue)+1 :
                    boundaryFlg = 1
            else:
                range_min = int(convertHexToDec(tmp_variableValidRange[:tmp_variableValidRange.find("~")]))
                range_max = int(convertHexToDec(tmp_variableValidRange[tmp_variableValidRange.find("~")+1:]))

                if value == range_min-1 :
                    boundaryFlg = 1
                elif value == range_min :
                    boundaryFlg = 1
                elif value == range_max :
                    boundaryFlg = 1
                elif value == range_max+1 :
                    boundaryFlg = 1
            variableValidRange = variableValidRange[variableValidRange.find(",")+1:]
            if endSearch == 1:
                break
    except ValueError:
        {}
    return boundaryFlg
    
    
    
def DuplicateChecker(index , injectionValue):
    duplicatedFlg = 0 
    for i in range(len(injectionValue)):
        if i!=index and injectionValue[index] == injectionValue[i]:
            duplicatedFlg = 1
    return duplicatedFlg
