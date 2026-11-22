"""Labs: experiments that run the same workload several ways and measure the difference.

engines.compare      a B+tree and an LSM tree on one write and read workload
buffer_pool.compare  buffer pool replacement policies on one recorded page-access trace
scaling.compare      TPC-H-style queries at doubling data sizes, and how each one's cost grows
access.compare       a B+tree, a hash index and a column store answering the same questions
"""
