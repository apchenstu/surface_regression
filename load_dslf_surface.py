import os
import torch
import numpy as np
import imageio 
import cv2
import time
from ctypes import *
from tqdm import tqdm as tqdm

class DslfSurfaceDataset(torch.utils.data.Dataset):
    def __init__(self, datadir, rdwrapper, list_ids, profile_file, limit=None, dsize=None):
        '''
        Initialize the surface light field. 
        @datadir: path to data of images, cameras, obj etc.
        @rdwrapper: a rd_wrapper instance for depth rendering
        @list_ids: list of image ids of training/testing images. The path
               of the corresponding image is {id}.0000.png
        @cam_file: file describing camera parameters
        @limit: If not None, specify the maximum number of pixels drawn 
            in each iteration. If you have a W=256,D=8 MLP, a limit of 
            400K would require roughly 6.4 GB of GPU memory
        @dsize: if not None, specify image training/testing resolution
        '''
        super(DslfSurfaceDataset).__init__()
        self.datadir = datadir
        self.list_ids = list_ids
        self.list_images = {}
        self.list_poses = {}
        self.list_Ks = {}
        self.labels = {}
        self.depths = {}
        self.limit = limit
        self.rdwrapper = rdwrapper
        self.x_trains = {}
        self.masks = {}

        extrinsic_file = None
        intrinsic_file = None
        list_file = None
        probed = False

        with open(os.path.join(datadir, profile_file), 'r') as fp:
            for line in fp:
                line = line.strip()
                if line == '':
                    continue
                parts = line.split(':')
                if parts[0] == 'camera_pose':
                    extrinsic_file = parts[1].strip()
                elif parts[0] == 'camera_intrinsic':
                    intrinsic_file = parts[1].strip()
                elif parts[0] == 'image_list':
                    list_file = parts[1].strip()
                
        for id in list_ids:
            with open(os.path.join(datadir, list_file), 'r') as f:
                for line in f:
                    line = line.strip()
                    parts = line.split()
                    if parts[0] == id:
                        image_file = parts[1].strip()
                        break
            
            with open(os.path.join(datadir, extrinsic_file), 'r') as f:
                for line in f:
                    line = line.strip()
                    parts = line.split(maxsplit=1)
                    if parts[0] == id:
                        extrinsic = parts[1].strip()
                        break

            with open(os.path.join(datadir, intrinsic_file), 'r') as f:
                for line in f:
                    line = line.strip()
                    parts = line.split(maxsplit=1)
                    if parts[0] == id:
                        intrinsic = parts[1].strip()
                        break
            
            # check image exists
            self.list_images[id] = os.path.join(datadir, image_file)
            if not os.path.exists(self.list_images[id]):
                raise Exception(f'{image_file} is not found')

            # parse camera poses
            M = np.zeros((16, 1))
            extrinsic = extrinsic.split()
            for i in range(16):
                M[i] = float(extrinsic[i])
            M = np.reshape(M, (4,4))
            M[:, 1] = -M[:, 1]  # y-axis differs
            M[:, 2] = -M[:, 2]  # so does z-axis
            self.list_poses[id] = M.astype(np.float32)

            # determine target image size
            if not probed:
                probe_image = imageio.imread(os.path.join(datadir, image_file))
                if not dsize:
                    self.H, self.W = probe_image.shape[:2]
                    self.H_scale, self.W_scale = (1., 1.)
                else:
                    self.H, self.W = dsize
                    self.H_scale, self.W_scale = (self.H/probe_image.shape[0], self.W/probe_image.shape[1])
                probed = True

            # parse and re-scale intrinsic
            M = np.zeros((9, 1))
            intrinsic = intrinsic.split()
            for i in range(9):
                M[i] = float(intrinsic[i])
            M = np.reshape(M, (3,3))
            M[0] *= self.W_scale
            M[1] *= self.H_scale
            self.list_Ks[id] = M.astype(np.float32)

        # pre-allocate labels
        print('Surface dataloader: loading images')
        for id in tqdm(list_ids):
            imagepath = self.list_images[id]
            I = imageio.imread(imagepath, pilmode='RGB')
            I = (np.array(I) / 255.).astype(np.float32)
            if not I.shape[:2] == (self.H, self.W):
                I = cv2.resize(I, (self.W, self.H), interpolation=cv2.INTER_LINEAR)
            self.labels[id] = I.reshape((-1, 3))

        # pre-render depths
        print('Surface dataloader: loading depths')
        for id in tqdm(list_ids):
            c2w = self.list_poses[id]
            K = self.list_Ks[id]
            depth = np.zeros((self.H * self.W, 1), dtype=np.float32)
            self.rdwrapper.render_depth(c2w, K, depth)
            self.depths[id] = depth

        # pre-calculate x-y-z-theta-phi and mask
        print('Surface dataloader: loading x-y-z-theta-phi')
        for id in tqdm(list_ids):
            self.x_trains[id], self.masks[id] = self.calculate_x_y_z_theta_phi(id)

    def calculate_x_y_z_theta_phi(self, id):
        # Select sample
        label = self.labels[id]
        depth = self.depths[id]
        c2w = self.list_poses[id]
        K = self.list_Ks[id]
        # imageio.imwrite(f'test.png', depth.reshape((self.H, self.W)))

        # constructing xyz coordinates
        ucoords = np.arange(self.W, dtype=np.float32)
        vcoords = np.arange(self.H, dtype=np.float32)
        uvd = np.concatenate([np.stack(np.meshgrid(ucoords, vcoords), -1).reshape(-1, 2), depth], axis=1)
        mask = uvd[:, 2] != 0.
        # If we have 4k input, mask.shape[0] would be a huge number
        # and easily blow up GPU memory. In order to avoid this issue, we 
        # pick randomly `limit` input samples (limit=400K would require 6.4 GB
        # of memory with MLP of size W=256, D=8). When evaluting, however, 
        # we need full pixels and don't do randomization
        base = np.sum(mask)
        if self.limit is not None and base > self.limit:
            # P(failing to pick one out of n in L times) = ((n-1)/n)**L
            L = int(np.log(self.limit/base) // np.log(1.-1./(self.W*self.H)))
            mask[np.random.randint(0, self.W*self.H, (L,))] = False
        uvd = uvd[mask]
        uvd[:,:2] = uvd[:,:2] * uvd[:,2:3]
        x_train = np.matmul(uvd, np.matmul(c2w[:3,:3], np.linalg.inv(K)).T) + c2w[:3, 3].T

        # Now x_train is a Mx3 matrix containing position coordinates, where M
        # denote # of non-zero pixels. Next we append directions to x_train
        dirs = x_train - c2w[:3, 3].T
        dirs = dirs / np.linalg.norm(dirs, axis=1, keepdims=True)
        x_train = np.hstack([x_train, dirs])
        # x_train = np.hstack((x_train, np.zeros_like(x_train[:,:2])))
        # xy = x_train[:,0]**2 + x_train[:,1]**2
        # x_train[:,3] = np.arctan2(np.sqrt(xy), x_train[:,2]) / np.pi # [0, pi] to [0, 1]
        # x_train[:,4] = np.arctan2(x_train[:,1], x_train[:,0]) / (2*np.pi) + 0.5 # [-pi, pi] to [0, 1]

        # Now we have x_train a Mx5 matrix containing position and rotation 
        # coordinates. The label can be easily obtained by taking the mask
        # of label, resulting a Mx3 matrix.
        return x_train, mask

    def __len__(self):
        return len(self.list_ids)

    def __getitem__(self, index):
        # Select sample
        id = self.list_ids[index]
        mask = self.masks[id]
        return self.x_trains[id], self.labels[id][mask], self.masks[id]
