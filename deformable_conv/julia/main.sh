#!/usr/bin/env bash

if [ $# != 1 ]; then
    echo "Usage: ./main.sh <cpu/gpu>"
    exit -1
fi

# Use the local version of CUDA
export JULIA_CUDA_USE_BINARYBUILDER=false

if [ $1 == 'cpu' ]; then
    threads=`cat /proc/cpuinfo | grep "processor" | wc -l`
    JULIA_NUM_THREADS=$threads julia cpu.jl
fi
