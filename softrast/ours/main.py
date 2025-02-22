import sys
import time
import itertools
import argparse
import numpy as np
import freetensor as ft
from freetensor import debug

sys.path.append('../..')
from common.numpy.io import load_txt, store_txt


def compile_all(h, w, n_verts, n_faces, device, ad_save_all):
    """
    Compute soft rasterization of each faces

    Suppose the points are already transposed, so we are viewing inside 0 <= x <= 1 and 0 <= y <= 1, along z-axis.
    The resolution along x and y is h and w, correspondingly.

    Output: An h*w*m-shaped tensor, where m is the number of faces, tensor[i, j, k] = the probability of face k at
    pixel (i, j)
    """

    sigma = 1e-4

    @ft.inline
    def cross_product(v1, v2):
        y = ft.empty((), "float32")
        y[()] = v1[0] * v2[1] - v1[1] * v2[0]
        return y

    @ft.inline
    def dot_product(v1, v2):
        y = ft.empty((), "float32")
        y[()] = v1[0] * v2[0] + v1[1] * v2[1]
        return y

    @ft.inline
    def norm(v):
        y = ft.empty((), "float32")
        y[()] = ft.sqrt(v[0] * v[0] + v[1] * v[1])
        return y

    @ft.inline
    def sub(v1, v2):
        y = ft.empty((2, ), "float32")
        y[0] = v1[0] - v2[0]
        y[1] = v1[1] - v2[1]
        return y

    @ft.transform
    def inference(vertices, faces, y):
        vertices: ft.Var[(n_verts, 3), "float32", "input"]
        faces: ft.Var[(n_faces, 3), "int32", "input"]
        y: ft.Var[(n_faces, h, w), "float32", "output"]
        #! nid: Li
        for i in range(n_faces):
            v = ft.empty((3, 2), "float32")
            for p in range(3):
                v[p, 0] = vertices[faces[i, p], 0]
                v[p, 1] = vertices[faces[i, p], 1]

            for j in range(h):
                for k in range(w):
                    pixel = ft.empty((2, ), "float32")
                    pixel[0] = 1. / (h - 1) * j
                    pixel[1] = 1. / (w - 1) * k

                    e_cp = ft.empty((3, ), "float32")
                    e_dist = ft.empty((3, ), "float32")
                    for p in range(3):
                        cp = cross_product(sub(pixel, v[p]),
                                           sub(v[(p + 1) % 3], v[p]))
                        e_cp[p] = cp[()]

                        dp1 = dot_product(sub(pixel, v[p]),
                                          sub(v[(p + 1) % 3], v[p]))
                        if dp1[()] >= 0:
                            dp2 = dot_product(sub(pixel, v[(p + 1) % 3]),
                                              sub(v[p], v[(p + 1) % 3]))
                            if dp2[()] >= 0:
                                len = norm(sub(v[(p + 1) % 3], v[p]))
                                e_dist[p] = ft.abs(cp[()]) / len[()]
                            else:
                                p2_dist = norm(sub(pixel, v[(p + 1) % 3]))
                                e_dist[p] = p2_dist[()]
                        else:
                            p1_dist = norm(sub(pixel, v[p]))
                            e_dist[p] = p1_dist[()]

                    inside = ft.empty((), "int32")
                    inside[()] = ft.if_then_else(
                        e_cp[0] < 0 and e_cp[1] < 0 and e_cp[2] < 0, 1, -1)
                    dist = ft.empty((), "float32")
                    dist[()] = ft.min(ft.min(e_dist[0], e_dist[1]), e_dist[2])
                    y[i, j,
                      k] = ft.sigmoid(inside[()] * dist[()] * dist[()] / sigma)

    print("# Inference:")
    print(inference)
    t0 = time.time()
    inference_exe = ft.optimize(
        inference,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)
    t1 = time.time()
    print(f"Inference compiling time: {t1 - t0}s")

    forward, backward, requires, privdes = ft.grad_(
        inference, set(["vertices"]), set(["y"]),
        ft.GradTapeMode.All if ad_save_all else ft.GradTapeMode.NoReuseOnly)

    print("# Forward:")
    print(forward)
    forward_exe = ft.optimize(
        forward,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)

    print("# Backward:")
    print(backward)
    backward_exe = ft.optimize(
        backward,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)

    def run_backward(vertices, faces, y, d_y, d_vertices):
        kvs = {}
        kvs[privdes['y']] = d_y
        kvs[requires['vertices']] = d_vertices
        backward_exe(**kvs)

    return inference_exe, forward_exe, run_backward


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?')
    parser.add_argument('--warmup-repeat',
                        type=int,
                        default=10,
                        dest='warmup_num')
    parser.add_argument('--timing-repeat',
                        type=int,
                        default=100,
                        dest='test_num')
    parser.add_argument('--ad-save-all',
                        action='store_true',
                        dest='ad_save_all')
    parser.add_argument('--profile-gpu',
                        action='store_true',
                        dest='profile_gpu')
    cmd_args = parser.parse_args()

    if cmd_args.profile_gpu:
        from common.gpu import profile_start, profile_stop

    device = cmd_args.target

    vertices = load_txt("../vertices.in", "float32")
    faces = load_txt("../faces.in", "int32")
    n_verts = vertices.shape[0]
    n_faces = faces.shape[0]
    h = 64
    w = 64
    y = np.zeros((n_faces, h, w), dtype="float32")
    d_vertices = np.zeros(vertices.shape, dtype='float32')
    d_y = load_txt("../d_y.in", "float32")

    if device == 'gpu':
        ir_dev = ft.Device(ft.GPU())
    else:
        assert device == 'cpu'
        ir_dev = ft.Device(ft.CPU())

    vertices = ft.Array(vertices)
    faces = ft.Array(faces)
    y = ft.Array(y)
    d_y = ft.Array(d_y)
    d_vertices = ft.Array(d_vertices)

    with ir_dev:
        inference, forward, backward = compile_all(h, w, n_verts, n_faces,
                                                   ir_dev,
                                                   cmd_args.ad_save_all)

    print(
        f"{cmd_args.warmup_num} warmup, {cmd_args.test_num} repeats for evalution"
    )
    warmup_num = cmd_args.warmup_num
    test_num = cmd_args.test_num

    for i in range(warmup_num):
        inference(vertices, faces, y)
        if i == 0:
            store_txt("y.out", y.numpy().reshape((n_faces, h, w)))
    ir_dev.sync()
    if cmd_args.profile_gpu:
        profile_start()
    t0 = time.time()
    for i in range(test_num):
        inference(vertices, faces, y)
    ir_dev.sync()
    t1 = time.time()
    if cmd_args.profile_gpu:
        profile_stop()

    print(f"Inference Time = {(t1 - t0) / test_num * 1000} ms")

    if cmd_args.profile_gpu:
        exit(0)

    for i in range(warmup_num):
        forward(vertices, faces, y)
    ir_dev.sync()
    t0 = time.time()
    for i in range(test_num):
        forward(vertices, faces, y)
    ir_dev.sync()
    t1 = time.time()

    print(f"Forward Time = {(t1 - t0) / test_num * 1000} ms")

    for i in range(warmup_num):
        backward(vertices, faces, y, d_y, d_vertices)
        if i == 0:
            store_txt("d_vertices.out",
                      d_vertices.numpy().reshape((n_verts, 3)))
    ir_dev.sync()
    t0 = time.time()
    for i in range(test_num):
        backward(vertices, faces, y, d_y, d_vertices)
    ir_dev.sync()
    t1 = time.time()

    print(f"Backward Time = {(t1 - t0) / test_num * 1000} ms")
