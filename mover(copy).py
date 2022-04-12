# -*- coding: utf-8 -*-
"""
Created on Sat Jul 10 09:31:20 2021

@author: Lovro
"""

import os
import shutil

#os.rename("path/to/current/file.foo", "path/to/new/destination/for/file.foo")
#shutil.move(r"E:\help\dwn8\LisaGenova_2021H_VO_Intro (mp3cut.net) (0).mp3", r"f:\3\LisaGenova_2021H_VO_Intro (mp3cut.net) (0)")
#os.replace("path/to/current/file.foo", "path/to/new/destination/for/file.foo")
for ii in range(0,130):
    str1="bla"+str(ii)
    print(str1)
#    shutil.move(r"H:\4dio\dr. Jozefina Škarica- Seminar za bračne parove IV.dio (mp3cut.net) ("+str(ii)+").mp3", r"f:\skarica4\part ("+str(ii)+").mp3")
    shutil.copy(r"E:\help\dwn8\yt5s.com - Knjiga o Jobu (128 kbps) (mp3cut.net) ("+str(ii)+").mp3", r"F:\job\do_1_40cca ("+str(ii)+").mp3")    

#shutil.copy(r"E:\help\dwn8\2022-03-20-15-06-46-mp3cutnet_4 ("+str(ii)+").mp3", r"F:\inventura\inv ("+str(ii)+").mp3")
