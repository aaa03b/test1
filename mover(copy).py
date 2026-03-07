# -*- coding: utf-8 -*-
"""
Created on Sat Jul 10 09:31:20 2021

@author: Lovro
"""

import os
import shutil
import argparse

parser = argparse.ArgumentParser(description="Copy or move files with a numbered pattern.")
parser.add_argument("--arg_in", required=True, help="Input file pattern with {i} placeholder, e.g. 'E:\\folder\\file ({i}).mp3'")
parser.add_argument("--arg_out", required=True, help="Output file pattern with {i} placeholder, e.g. 'F:\\dest\\file ({i}).mp3'")
parser.add_argument("--action", choices=["copy", "move"], default="copy", help="Action to perform: copy (default) or move")
parser.add_argument("--start", type=int, default=0, help="Start index (default: 0)")
parser.add_argument("--end", type=int, default=130, help="End index exclusive (default: 130)")
args = parser.parse_args()

for ii in range(args.start, args.end):
    src = args.arg_in.replace("{i}", str(ii))
    dst = args.arg_out.replace("{i}", str(ii))
    print(f"{args.action}: {src} -> {dst}")
    if args.action == "copy":
        shutil.copy(src, dst)
    else:
        shutil.move(src, dst)
