#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gdb
import subprocess
import re
import copy

# main_arena
main_arena = 0
main_arena_off = 0 

# chunks
top = {}
fastbinsize = 10
fastbin = []
last_remainder = {}
unsortbin = []
smallbin = {}  #{size:bin}
largebin = {}

# chunk recording
freememoryarea = {} #using in parse
allocmemoryarea = {}
freerecord = {} # using in trace

# setting for tracing memory allocation
tracelargebin = True
inmemalign = False
inrealloc = False
print_overlap = True
DEBUG = True  #debug msg (free and malloc) if you want

# breakpoints for tracing 
mallocbp = None
freebp = None
memalignbp = None
reallocbp = None

# architecture setting
capsize = 0
word = ""
arch = ""

def init_angelheap():
    global allocmemoryarea
    global freerecord
    
    dis_trace_malloc()
    allocmemoryarea = {}
    freerecord = {} 

class Malloc_bp_ret(gdb.FinishBreakpoint):
    global allocmemoryarea
    global freerecord
    
    def __init__(self,arg):
        gdb.FinishBreakpoint.__init__(self,gdb.newest_frame(),internal=True)
        self.silent = True
        self.arg = arg
    
    def stop(self):
        chunk = {}
        if len(arch) == 0 :
            getarch()
        if arch == "x86-64" :
            value = int(self.return_value)
            chunk["addr"] = value - capsize*2
        else :
            cmd = "info register $eax"
            value = int(gdb.execute(cmd,to_string=True).split()[1].strip(),16)
            chunk["addr"] = value - capsize*2
        if value == 0 :
            return False
    
        cmd = "x/" + word + hex(chunk["addr"] + capsize)
        chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
        overlap,status = check_overlap(chunk["addr"],chunk["size"],allocmemoryarea)
        if overlap and status == "error" :
            if DEBUG :
                print("\033[34m>--------------------------------------------------------------------------------------<\033[37m")
                msg = "\033[33mmalloc(0x%x)\033[37m" % self.arg
                print("%-40s = 0x%x \033[31m overlap detected !! (0x%x)\033[37m" % (msg,chunk["addr"]+capsize*2,overlap["addr"]))
                print("\033[34m>--------------------------------------------------------------------------------------<\033[37m")
            else :
                print("\033[31moverlap detected !! (0x%x)\033[37m" % overlap["addr"])
            del allocmemoryarea[hex(overlap["addr"])]
        else :
            if DEBUG:
                msg = "\033[33mmalloc(0x%x)\033[37m" % self.arg
                print("%-40s = 0x%x" % (msg,chunk["addr"] + capsize*2))
        allocmemoryarea[hex(chunk["addr"])] = copy.deepcopy((chunk["addr"],chunk["addr"]+chunk["size"],chunk))
        if hex(chunk["addr"]) in freerecord :
            freechunktuple = freerecord[hex(chunk["addr"])]
            freechunk = freechunktuple[2]
            splitchunk = {}
            del freerecord[hex(chunk["addr"])]
            if chunk["size"] != freechunk["size"] :
                splitchunk["addr"] = chunk["addr"] + chunk["size"]
                splitchunk["size"] = freechunk["size"] - chunk["size"]
                freerecord[hex(splitchunk["addr"])] = copy.deepcopy((splitchunk["addr"],splitchunk["addr"]+splitchunk["size"],splitchunk))
        if self.arg >= 128*capsize :
            Malloc_consolidate()

class Malloc_Bp_handler(gdb.Breakpoint):
    def stop(self):
        if len(arch) == 0 :
            getarch()
        if arch == "x86-64":
            reg = "$rsi"
            arg = int(gdb.execute("info register " + reg,to_string=True).split()[1].strip(),16)
        else :
            # for _int_malloc in x86's glibc (unbuntu 14.04 & 16.04), size is stored in edx
            reg = "$edx"
            arg = int(gdb.execute("info register " + reg,to_string=True).split()[1].strip(),16)
        Malloc_bp_ret(arg)
        return False

class Free_bp_ret(gdb.FinishBreakpoint):
    def __init__(self):
        gdb.FinishBreakpoint.__init__(self,gdb.newest_frame(),internal=True)
        self.silent = True
    
    def stop(self):
        Malloc_consolidate()
        return False

class Free_Bp_handler(gdb.Breakpoint):

    def stop(self):
        global allocmemoryarea
        global freerecord
        global inmemalign
        global inrealloc
        get_top_lastremainder()

        if len(arch) == 0 :
            getarch()
        if arch == "x86-64":
            reg = "$rsi"
            result = int(gdb.execute("info register " + reg,to_string=True).split()[1].strip(),16) + 0x10
        else :
            # for _int_free in x86's glibc (unbuntu 14.04 & 16.04), chunk address is stored in edx
            reg = "$edx"
            result = int(gdb.execute("info register " + reg,to_string=True).split()[1].strip(),16) + 0x8
        chunk = {}
        if inmemalign or inrealloc:
            Update_alloca()
            inmemalign = False
            inrealloc = False
        prevfreed = False
        chunk["addr"] = result - capsize*2

        cmd = "x/" +word + hex(chunk["addr"] + capsize)
        size = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
        chunk["size"] = size & 0xfffffffffffffff8
        if (size & 1) == 0 :
            prevfreed = True
#        overlap,status = check_overlap(chunk["addr"],chunk["size"],freememoryarea)
        overlap,status = check_overlap(chunk["addr"],chunk["size"],freerecord)
        if overlap and status == "error" :
            if DEBUG :
                msg = "\033[32mfree(0x%x)\033[37m (size = 0x%x)" % (result,chunk["size"])
                print("\033[34m>--------------------------------------------------------------------------------------<\033[37m")
                print("%-25s \033[31m double free detected !! (0x%x(size:0x%x))\033[37m" % (msg,overlap["addr"],overlap["size"]))
                print("\033[34m>--------------------------------------------------------------------------------------<\033[37m",end="")
            else :
                print("\033[31mdouble free detected !! (0x%x)\033[37m" % overlap["addr"])
            del freerecord[hex(overlap["addr"])]
        else :
            if DEBUG :
                msg = "\033[32mfree(0x%x)\033[37m" % result
                print("%-40s (size = 0x%x)" % (msg,chunk["size"]),end="")

        if chunk["size"] <= 0x80 :
            freerecord[hex(chunk["addr"])] = copy.deepcopy((chunk["addr"],chunk["addr"]+chunk["size"],chunk))
            if DEBUG :
                print("")
            if hex(chunk["addr"]) in allocmemoryarea :
                del allocmemoryarea[hex(chunk["addr"])]
            return False

        prevchunk = {}
        if prevfreed :
            cmd = "x/" +word + hex(chunk["addr"])
            prevchunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
            prevchunk["addr"] = chunk["addr"] - prevchunk["size"]
            if hex(prevchunk["addr"]) not in freerecord :
                print("\033[31m confuse in prevchunk 0x%x" % prevchunk["addr"])
            else :
                prevchunk["size"] += chunk["size"]
                del freerecord[hex(prevchunk["addr"])]

        nextchunk = {}
        nextchunk["addr"] = chunk["addr"] + chunk["size"]
        
        if nextchunk["addr"] == top["addr"] :
            if hex(chunk["addr"]) in allocmemoryarea :
                del allocmemoryarea[hex(chunk["addr"])]
                Free_bp_ret()
            if DEBUG :
                print("")
            return False

        cmd = "x/" + word + hex(nextchunk["addr"] + capsize)
        nextchunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
        cmd = "x/" + word + hex(nextchunk["addr"] + nextchunk["size"] + capsize)
        nextinused = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 1
        
        if nextinused == 0 and prevfreed: #next chunk is freed                       
            if hex(nextchunk["addr"]) not in freerecord :
                print("\033[31m confuse in nextchunk 0x%x" % nextchunk["addr"])
            else :
                prevchunk["size"] += nextchunk["size"]
                del freerecord[hex(nextchunk["addr"])]
        if nextinused == 0 and not prevfreed:
            if hex(nextchunk["addr"]) not in freerecord :
                print("\033[31m confuse in nextchunk 0x%x" % nextchunk["addr"])
            else :
                chunk["size"] += nextchunk["size"]
                del freerecord[hex(nextchunk["addr"])]
        if prevfreed :
            if hex(chunk["addr"]) in allocmemoryarea :
                del allocmemoryarea[hex(chunk["addr"])]
            chunk = prevchunk

        if DEBUG :
            print("")
        freerecord[hex(chunk["addr"])] = copy.deepcopy((chunk["addr"],chunk["addr"]+chunk["size"],chunk))
        if hex(chunk["addr"]) in allocmemoryarea :
            del allocmemoryarea[hex(chunk["addr"])]
        if chunk["size"] > 65536 :
            Malloc_consolidate()
        return False

class Memalign_Bp_handler(gdb.Breakpoint):
    def stop(self):
        global inmemalign
        inmemalign = True
        return False

class Realloc_Bp_handler(gdb.Breakpoint):
    def stop(self):
        global inrealloc
        inrealloc = True
        return False

def Update_alloca():
    global allocmemoryarea
    if capsize == 0:
        getarch()
    for addr,(start,end,chunk) in allocmemoryarea.items():
        cmd = "x/" + word + hex(chunk["addr"] + capsize*1)
        cursize = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8

        if cursize != chunk["size"]:
            chunk["size"] = cursize
            allocmemoryarea[hex(chunk["addr"])]= copy.deepcopy((start,start + cursize,chunk))
            

def Malloc_consolidate(): #merge fastbin when malloc a large chunk or free a very large chunk
    global fastbin
    global freerecord

    if capsize == 0 :
        getarch()
    freerecord = {}
    get_heap_info()
    freerecord = copy.deepcopy(freememoryarea) 

def getarch():
    global capsize
    global word
    global arch

    data = gdb.execute('show arch',to_string = True)
    tmp =  re.search("currently.*",data)
    if tmp :
        info = tmp.group()
        if "x86-64" in info:
            capsize = 8
            word = "gx "
            arch = "x86-64"
            return "x86-64"
        elif "aarch64" in info :
            capsize = 8
            word = "gx "
            arch = "aarch64"
            return "aarch64"
        elif "arm" in info :
            capsize = 4
            word = "wx "
            arch = "arm"
            return "arm"
        else :
            word = "wx "
            capsize = 4
            arch = "i386"
            return  "i386"
    else :
        return "error"


def procmap():
    data = gdb.execute('info proc exe',to_string = True)
    pid = re.search('process.*',data)
    if pid :
        pid = pid.group()
        pid = pid.split()[1]
        maps = open("/proc/" + pid + "/maps","r")
        infomap = maps.read()
        maps.close()
        return infomap
    else :
        return "error"

def libcbase():
    infomap = procmap()
    data = re.search(".*libc.*\.so",infomap)
    if data :
        libcaddr = data.group().split("-")[0]
        return int(libcaddr,16)
    else :
        return 0

def getoff(sym):
    libc = libcbase()
    if type(sym) is int :
        return sym-libc
    else :
        try :
            data = gdb.execute("x/x " + sym ,to_string=True)
            if "No symbol" in data:
                return 0
            else :
                data = re.search("0x.*[0-9a-f] ",data)
                data = data.group()
                symaddr = int(data[:-1] ,16)
                return symaddr-libc
        except :
            return 0

def set_main_arena():
    global main_arena
    global main_arena_off

    offset = getoff("&main_arena")
    if offset == 0: # no main_arena symbol
        print("Cannot get main_arena's symbol address. Make sure you install libc debug file (libc6-dbg & libc6-dbg:i386 for debian package).")
        return
    libc = libcbase()
    arch = getarch()
    main_arena_off = offset
    main_arena = libc + main_arena_off

def check_overlap(addr,size,data = None):
    if data :
        for key,(start,end,chunk) in data.items() :
            if (addr >= start and addr < end) or ((addr+size) > start and (addr+size) < end ) or ((addr < start) and  ((addr + size) >= end)):
                return chunk,"error"
    else :
        for key,(start,end,chunk) in freememoryarea.items() :
    #    print("addr 0x%x,start 0x%x,end 0x%x,size 0x%x" %(addr,start,end,size) )
            if (addr >= start and addr < end) or ((addr+size) > start and (addr+size) < end ) or ((addr < start) and  ((addr + size) >= end)):
                return chunk,"freed"
        for key,(start,end,chunk) in allocmemoryarea.items() :
    #    print("addr 0x%x,start 0x%x,end 0x%x,size 0x%x" %(addr,start,end,size) )
            if (addr >= start and addr < end) or ((addr+size) > start and (addr+size) < end ) or ((addr < start) and  ((addr + size) >= end)) :
                return chunk,"inused" 
    return None,None

def get_top_lastremainder():
    global main_arena
    global fastbinsize
    global top
    global last_remainder

    chunk = {}
    if capsize == 0 :
        arch = getarch()
    #get top
    cmd = "x/" + word + hex(main_arena + fastbinsize*capsize + 8 )
    chunk["addr"] =  int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
    chunk["size"] = 0
    if chunk["addr"] :
        cmd = "x/" + word + hex(chunk["addr"]+capsize*1)
        try :
            chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
            if chunk["size"] > 0x21000 :
                chunk["memerror"] = "top is broken ?"
        except :
            chunk["memerror"] = "invaild memory"
    top = copy.deepcopy(chunk)
    #get last_remainder
    chunk = {}
    cmd = "x/" + word + hex(main_arena + (fastbinsize+1)*capsize + 8 )
    chunk["addr"] =  int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
    chunk["size"] = 0
    if chunk["addr"] :
        cmd = "x/" + word + hex(chunk["addr"]+capsize*1)
        try :
            chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
        except :
            chunk["memerror"] = "invaild memory"
    last_remainder = copy.deepcopy(chunk)

def get_fast_bin():
    global main_arena
    global fastbin
    global fastbinsize
    global freememoryarea

    fastbin = []
    #freememoryarea = []
    if capsize == 0 :
        arch = getarch()
    for i in range(fastbinsize-3):
        fastbin.append([])
        chunk = {}
        is_overlap = (None,None)
        cmd = "x/" + word  + hex(main_arena + i*capsize + 8)
        chunk["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
        while chunk["addr"] and not is_overlap[0]:
            cmd = "x/" + word + hex(chunk["addr"]+capsize*1)
            try :
                chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
            except :
                chunk["memerror"] = "invaild memory"
                break
            is_overlap = check_overlap(chunk["addr"], (capsize*2)*(i+2))
            chunk["overlap"] = is_overlap
            freememoryarea[hex(chunk["addr"])] = copy.deepcopy((chunk["addr"],chunk["addr"] + (capsize*2)*(i+2) ,chunk))
            fastbin[i].append(copy.deepcopy(chunk))
            cmd = "x/" + word + hex(chunk["addr"]+capsize*2)
            chunk = {}
            chunk["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
        if not is_overlap[0]:
            chunk["size"] = 0
            chunk["overlap"] = None
            fastbin[i].append(copy.deepcopy(chunk))

def trace_normal_bin(chunkhead):
    global main_arena
    global freememoryarea 

    libc = libcbase()
    bins = []
    if capsize == 0 :
        arch = getarch()
    if chunkhead["addr"] == 0 : # main_arena not initial
        return None
    chunk = {}
    cmd = "x/" + word  + hex(chunkhead["addr"] + capsize*2) #fd
    chunk["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) #get fd chunk
    if (chunk["addr"] == chunkhead["addr"]) :  #no chunk in the bin
        if (chunkhead["addr"] > libc) :
            return bins
        else :
            try :
                cmd = "x/" + word + hex(chunk["addr"]+capsize*1)
                chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
                is_overlap = check_overlap(chunk["addr"],chunk["size"])
                chunk["overlap"] = is_overlap
                chunk["memerror"] = "\033[31mbad fd (" + hex(chunk["addr"]) + ")\033[37m"
            except :
                chunk["memerror"] = "invaild memory"
            bins.append(copy.deepcopy(chunk)) 
            return bins
    else :
        try :
            cmd = "x/" + word + hex(chunkhead["addr"]+capsize*3)
            bk = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
            cmd = "x/" + word + hex(bk+capsize*2)
            bk_fd = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
            if bk_fd != chunkhead["addr"]:
                chunkhead["memerror"] = "\033[31mdoubly linked list corruption {0} != {1} and \033[36m{2}\033[31m is broken".format(hex(chunkhead["addr"]),hex(bk_fd),hex(chunkhead["addr"]))
                bins.append(copy.deepcopy(chunkhead))
                return bins
            fd = chunkhead["addr"]
            chunkhead = {}
            chunkhead["addr"] = bk #bins addr
            chunk["addr"] = fd #first chunk
        except :
            chunkhead["memerror"] = "invaild memory" 
            bins.append(copy.deepcopy(chunkhead))
            return bins
        while chunk["addr"] != chunkhead["addr"] :
            try :
                cmd = "x/" + word + hex(chunk["addr"])
                chunk["prev_size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
                cmd = "x/" + word + hex(chunk["addr"]+capsize*1)
                chunk["size"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16) & 0xfffffffffffffff8
            except :
                chunk["memerror"] = "invaild memory"
                break
            try :
                cmd = "x/" + word + hex(chunk["addr"]+capsize*2)
                fd = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
                if fd == chunk["addr"] :
                    chunk["memerror"] = "\033[31mbad fd (" + hex(fd) + ")\033[37m"
                    bins.append(copy.deepcopy(chunk))
                    break
                cmd = "x/" + word + hex(fd + capsize*3)
                fd_bk = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
                if chunk["addr"] != fd_bk :
                    chunk["memerror"] = "\033[31mdoubly linked list corruption {0} != {1} and \033[36m{2}\033[31m or \033[36m{3}\033[31m is broken".format(hex(chunk["addr"]),hex(fd_bk),hex(fd),hex(chunk["addr"]))
                    bins.append(copy.deepcopy(chunk))
                    break
            except :
                chunk["memerror"] = "invaild memory"
                bins.append(copy.deepcopy(chunk))
                break
            is_overlap = check_overlap(chunk["addr"],chunk["size"])
            chunk["overlap"] = is_overlap
            freememoryarea[hex(chunk["addr"])] = copy.deepcopy((chunk["addr"],chunk["addr"] + chunk["size"] ,chunk))
            bins.append(copy.deepcopy(chunk))
            cmd = "x/" + word + hex(chunk["addr"]+capsize*2) #find next
            chunk = {}
            chunk["addr"] = fd
    return bins

def get_unsortbin():
    global main_arena
    global unsortbin
    unsortbin = []
    if capsize == 0 :
        arch = getarch()
    chunkhead = {}
    cmd = "x/" + word + hex(main_arena + (fastbinsize+2)*capsize+8)
    chunkhead["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
    unsortbin = trace_normal_bin(chunkhead)

def get_smallbin():
    global main_arena
    global smallbin

    smallbin = {}
    if capsize == 0 :
        arch = getarch()
    max_smallbin_size = 512*int(capsize/4)
    for size in range(capsize*4,max_smallbin_size,capsize*2):
        chunkhead = {}
        idx = int((size/(capsize*2)))-1
        cmd = "x/" + word + hex(main_arena + (fastbinsize+2)*capsize+8 + idx*capsize*2)  # calc the smallbin index
        chunkhead["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
        bins = trace_normal_bin(chunkhead)
        if bins and len(bins) > 0 :
            smallbin[hex(size)] = copy.deepcopy(bins)

def largbin_index(size):
    if capsize == 0 :
        arch = getarch()
    if capsize == 8 :
        if (size >> 6) <= 48 :
            idx = 48 + (size >> 6)
        elif (size >> 9) <= 20 :
            idx = 91 + (size >> 9)
        elif (size >> 12) <= 10:
            idx = 110 + (size >> 12)
        elif (size >> 15) <= 4 :
            idx = 119 + (size >> 15)
        elif (size >> 18) <= 2:
            idx = 124 + (size >> 18)
        else :
            idx = 126
    else :
        if (size >> 6) <= 38 :
            idx = 56 + (size >> 6)
        elif (size >> 9) <= 20 :
            idx = 91 + (size >> 9)
        elif (size >> 12) <= 10:
            idx = 110 + (size >> 12)
        elif (size >> 15) <= 4 :
            idx = 119 + (size >> 15)
        elif (size >> 18) <= 2:
            idx = 124 + (size >> 18)
        else :
            idx = 126
    return idx 

def get_largebin():
    global main_arena
    global largebin

    largebin = {}
    if capsize == 0 :
        arch = getarch()
    min_largebin = 512*int(capsize/4)
    for idx in range(64,128):
        chunkhead = {}
        cmd = "x/" + word + hex(main_arena + (fastbinsize+2)*capsize + idx*capsize*2)  # calc the largbin index
        chunkhead["addr"] = int(gdb.execute(cmd,to_string=True).split(":")[1].strip(),16)
        bins = trace_normal_bin(chunkhead)
        if bins and len(bins) > 0 :
            largebin[idx] = copy.deepcopy(bins)

def get_heap_info():
    global main_arena
    global freememoryarea
    global top

    top = {}
    freememoryarea = {}
    set_main_arena()
    if main_arena :
        get_unsortbin()
        get_smallbin()
        if tracelargebin :
            get_largebin()
        get_fast_bin()
        get_top_lastremainder()

def get_reg(reg):
    cmd = "info register " + reg
    result = int(gdb.execute(cmd,to_string=True).split()[1].strip(),16)
    return result

def trace_malloc():
    global mallocbp
    global freebp
    global memalignbp
    global reallocbp
    
    mallocbp = Malloc_Bp_handler("*" + "_int_malloc")
    freebp = Free_Bp_handler("*" + "_int_free")
    memalignbp = Memalign_Bp_handler("*" + "_int_memalign")
    reallocbp = Realloc_Bp_handler("*" + "_int_realloc")
    get_heap_info()

def dis_trace_malloc():
    global mallocbp
    global freebp
    global memalignbp
    global reallocbp

    if mallocbp :
        mallocbp.delete()
        mallocbp = None
    if freebp :   
        freebp.delete()
        freebp = None
    if memalignbp :
        memalignbp.delete()
        memalignbp = None
    if reallocbp :
        reallocbp.delete()
        reallocbp = None
 
def find_overlap(chunk,bins):
    is_overlap = False
    count = 0
    for current in bins :
        if chunk["addr"] == current["addr"] :
            count += 1
    if count > 1 :
        is_overlap = True
    return is_overlap

def putfastbin():
    if capsize == 0 :
        arch = getarch()

    get_heap_info()
    for i,bins in enumerate(fastbin) :
        cursize = (capsize*2)*(i+2)
        print("\033[32m(0x%02x)     fastbin[%d]:\033[37m " % (cursize,i),end = "")
        for chunk in bins :
            if "memerror" in chunk :
                print("\033[31m0x%x (%s)\033[37m" % (chunk["addr"],chunk["memerror"]),end = "")
            elif chunk["size"] != cursize and chunk["addr"] != 0 :
                print("\033[36m0x%x (size error (0x%x))\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            elif chunk["overlap"] and chunk["overlap"][0]:
                print("\033[31m0x%x (overlap chunk with \033[36m0x%x(%s)\033[31m )\033[37m" % (chunk["addr"],chunk["overlap"][0]["addr"],chunk["overlap"][1]),end = "")
            elif chunk == bins[0]  :
                print("\033[34m0x%x\033[37m" % chunk["addr"],end = "")
            else  :
                if print_overlap :
                    if find_overlap(chunk,bins):
                        print("\033[31m0x%x\033[37m" % chunk["addr"],end ="")
                    else :
                        print("0x%x" % chunk["addr"],end = "")
                else :
                    print("0x%x" % chunk["addr"],end = "")
            if chunk != bins[-1]:
                print(" --> ",end = "")
        print("")

def putheapinfo():
    if capsize == 0 :
        arch = getarch()

    putfastbin()
    if "memerror" in top :
        print("\033[35m %20s:\033[31m 0x%x \033[33m(size : 0x%x)\033[31m (%s)\033[37m " % ("top",top["addr"],top["size"],top["memerror"]))
    else :
        print("\033[35m %20s:\033[34m 0x%x \033[33m(size : 0x%x)\033[37m " % ("top",top["addr"],top["size"]))

    print("\033[35m %20s:\033[34m 0x%x \033[33m(size : 0x%x)\033[37m " % ("last_remainder",last_remainder["addr"],last_remainder["size"]))
    if unsortbin and len(unsortbin) > 0 :
        print("\033[35m %20s:\033[37m " % "unsortbin",end="")
        for chunk in unsortbin :
            if "memerror" in chunk :
                print("\033[31m0x%x (%s)\033[37m" % (chunk["addr"],chunk["memerror"]),end = "")
            elif chunk["overlap"] and chunk["overlap"][0]:
                print("\033[31m0x%x (overlap chunk with \033[36m0x%x(%s)\033[31m )\033[37m" % (chunk["addr"],chunk["overlap"][0]["addr"],chunk["overlap"][1]),end = "")
            elif chunk == unsortbin[-1]:
                print("\033[34m0x%x\033[37m \33[33m(size : 0x%x)\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            else :
                print("0x%x \33[33m(size : 0x%x)\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            if chunk != unsortbin[-1]:
                print(" <--> ",end = "")
        print("")
    else :
        print("\033[35m %20s:\033[37m 0x%x" % ("unsortbin",0)) #no chunk in unsortbin
    for size,bins in smallbin.items() :
        idx = int((int(size,16)/(capsize*2)))-2 
        print("\033[33m(0x%03x)  %s[%2d]:\033[37m " % (int(size,16),"smallbin",idx),end="")
        for chunk in bins :
            if "memerror" in chunk :
                print("\033[31m0x%x (%s)\033[37m" % (chunk["addr"],chunk["memerror"]),end = "")
            elif chunk["size"] != int(size,16) :
                print("\033[36m0x%x (size error (0x%x))\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            elif chunk["overlap"] and chunk["overlap"][0]:
                print("\033[31m0x%x (overlap chunk with \033[36m0x%x(%s)\033[31m )\033[37m" % (chunk["addr"],chunk["overlap"][0]["addr"],chunk["overlap"][1]),end = "")
            elif chunk == bins[-1]:
                print("\033[34m0x%x\033[37m" % chunk["addr"],end = "")
            else :
                print("0x%x " % chunk["addr"],end = "")
            if chunk != bins[-1]:
                print(" <--> ",end = "")
        print("") 
    for idx,bins in largebin.items():
#        print("\033[33m(0x%03x-0x%03x)  %s[%2d]:\033[37m " % (int(size,16),int(maxsize,16),"largebin",idx),end="")
        print("\033[33m  %15s[%2d]:\033[37m " % ("largebin",idx),end="")
        for chunk in bins :
            if "memerror" in chunk :
                print("\033[31m0x%x (%s)\033[37m" % (chunk["addr"],chunk["memerror"]),end = "")
            elif chunk["overlap"] and chunk["overlap"][0]:
                print("\033[31m0x%x (overlap chunk with \033[36m0x%x(%s)\033[31m )\033[37m" % (chunk["addr"],chunk["overlap"][0]["addr"],chunk["overlap"][1]),end = "")
            elif largbin_index(chunk["size"]) != idx : 
                print("\033[31m0x%x (incorrect bin size :\033[36m 0x%x\033[31m)\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            elif chunk == bins[-1]:
                print("\033[34m0x%x\033[37m \33[33m(size : 0x%x)\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            else :
                print("0x%x \33[33m(size : 0x%x)\033[37m" % (chunk["addr"],chunk["size"]),end = "")
            if chunk != bins[-1]:
                print(" <--> ",end = "")
        print("") 

def putinused():
    print("\033[33m %s:\033[37m " % "inused ",end="")
    for addr,(start,end,chunk) in allocmemoryarea.items() :
        print("0x%x," % (chunk["addr"]),end="")
    print("")
