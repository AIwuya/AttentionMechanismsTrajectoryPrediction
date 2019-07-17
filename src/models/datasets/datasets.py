import torch
from torch.utils import data
# from classes.pretrained_vgg import customCNN1

import cv2
import numpy as np 
import json
import h5py
import cv2
import time
import helpers.helpers_training as helpers
# import helpers
from joblib import load
import sys
from PIL import Image
from torchvision import transforms

class CustomDataLoader():
      def __init__(self,batch_size,shuffle,drop_last,dataset,test = 0):
            self.shuffle = shuffle 
            self.dataset = dataset
            self.data_len = self.dataset.get_len()

            self.batch_size = batch_size
            self.drop_last = drop_last
            self.test = test
            self.split_batches()

      def split_batches(self):
            self.batches = list(torch.utils.data.BatchSampler(
                  torch.utils.data.RandomSampler(range(self.data_len)),
                  batch_size = self.batch_size,
                  drop_last =self.drop_last))
            self.batch_idx = 0
            self.nb_batches = len(self.batches)
            if self.test :
                  self.nb_batches = 30

            

      def __iter__(self):
            return self
      def __next__(self):

            if self.batch_idx >= self.nb_batches:
                  self.split_batches()
                  raise StopIteration
            else:     
                  ids = sorted(self.batches[self.batch_idx])
                  self.batch_idx += 1 
                  return self.dataset.get_ids(ids)



"""
      set_type:  train eval  test train_eval
      use_images: True False
      use_neighbors: True False
      predict_offsets: 0: none, 1: based on last obs point, 2: based on previous point

      data_type: frames trajectories
"""
class Hdf5Dataset():
      'Characterizes a dataset for PyTorch'
      def __init__(self,padding,hdf5_file,scene_list,t_obs,t_pred,set_type, data_type,use_neighbors,
                  use_masks = False,reduce_batches = True, predict_offsets = 0,offsets_input = 0,evaluation = 0):

                       
            self.set_type = set_type
            self.scene_list = scene_list

            self.data_type = data_type
            self.use_neighbors = use_neighbors
            self.use_masks = use_masks

            self.evaluation = evaluation

            self.reduce_batches = reduce_batches
            self.predict_offsets = predict_offsets
            self.offsets_input = offsets_input

            self.hdf5_file = h5py.File(hdf5_file,"r")

            if self.evaluation:
                  self.dset_name = self.scene_list[0]
                  self.dset_types = "{}_types".format(self.scene_list[0])
                  self.coord_dset = self.hdf5_file[self.data_type][self.dset_name]
                  self.types_dset = self.hdf5_file[self.data_type][self.dset_types]  

            else: 
                  self.dset_name = "samples_{}_{}".format(set_type,data_type)
                  self.dset_types = "types_{}_{}".format(set_type,data_type)   
                  self.coord_dset = self.hdf5_file[self.dset_name]  
                  self.types_dset = self.hdf5_file[self.dset_types]  
            
            self.t_obs = t_obs
            self.t_pred = t_pred
            self.seq_len = t_obs + t_pred
            self.padding = padding
                     

            self.shape = self.coord_dset.shape
            
      def __del__(self):
            self.hdf5_file.close()
      def get_len(self):
            return self.shape[0]

      


      def get_ids(self,ids):
            types,X,y,seq = [],[],[],[]
            max_batch = self.coord_dset.shape[1]
 
            
            seq = self.coord_dset[ids]
            X = seq[:,:,:self.t_obs]
            y = seq[:,:,self.t_obs:self.seq_len]

            # compute max nb of agents in a frame
            if self.reduce_batches:
                  max_batch = self.__get_batch_max_neighbors(X)

            X = X[:,:max_batch]
            y = y[:,:max_batch]
            seq = seq[:,:max_batch]

            if self.use_neighbors:
                  types = self.types_dset[ids,:max_batch] #B,N,tpred,2
            else:
                  types =  self.types_dset[ids,0] #B,1,tpred,2
                  


            points_mask = []
            if self.use_neighbors:
                  X,y,points_mask,y_last,X_last = self.__get_x_y_neighbors(X,y,seq)
            else:       
                  X,y,points_mask,y_last,X_last = self.__get_x_y(X,y,seq)                      


            sample_sum = (np.sum(points_mask[1].reshape(points_mask[1].shape[0],points_mask[1].shape[1],-1), axis = 2) > 0).astype(int)
            active_mask = np.argwhere(sample_sum.flatten()).flatten()


            out = [
                  torch.FloatTensor(X).contiguous(),
                  torch.FloatTensor(y).contiguous(),
                  torch.FloatTensor(types)
            ]   
            if self.use_masks:
                  out.append(points_mask)
                  out.append(torch.LongTensor(active_mask))
            
            out.append(y_last)
            out.append(X_last)

            return tuple(out)


      def __get_batch_max_neighbors(self,X):
           
            active_mask = (X == self.padding).astype(int)
            a = np.sum(active_mask,axis = 3)
            b = np.sum( a, axis = 2)
            nb_padding_traj = b/float(2.0*self.t_obs) #prop of padded points per traj
            active_traj = nb_padding_traj < 1.0 # if less than 100% of the points are padding points then its an active trajectory
            nb_agents = np.sum(active_traj.astype(int),axis = 1)                      
            max_batch = np.max(nb_agents)

            return max_batch

           
      def __get_x_y_neighbors(self,X,y,seq):
            active_mask = (y != self.padding).astype(int)    
            active_mask_in = (X != self.padding).astype(int)            
            active_last_points = []
            original_x = []

            if self.predict_offsets:
                  if self.predict_offsets == 1:
                        # offsets according to last obs point, take last point for each obs traj and make it an array of dimension y
                        last_points = np.repeat(  np.expand_dims(X[:,:,-1],2),  self.t_pred, axis=2)#B,N,tpred,2
                  elif self.predict_offsets == 2:# y shifted left

                        # offsets according to preceding point point, take points for tpred shifted 1 timestep left
                        last_points = seq[:,:,self.t_obs-1:self.seq_len-1]


                  
                  active_last_points = np.multiply(active_mask,last_points)
                  y = np.subtract(y,active_last_points)
            if self.offsets_input:
                  first_points = np.concatenate([np.expand_dims(X[:,:,0],2), X[:,:,0:self.t_obs-1]], axis = 2)
                  active_first_points = np.multiply(active_mask_in,first_points)
                  original_x = X
                  original_x = np.multiply(original_x,active_mask_in) # put padding to 0

                  
                  X = np.subtract(X,active_first_points)


            y = np.multiply(y,active_mask) # put padding to 0
            X = np.multiply(X,active_mask_in) # put padding to 0
            
            return X,y,(active_mask_in,active_mask),active_last_points,original_x 

      def __get_x_y(self,X,y,seq):

            X = np.expand_dims( X[:,0] ,1) # keep only first neighbors and expand nb_agent dim 
            y = np.expand_dims( y[:,0], 1) #B,1,tpred,2 # keep only first neighbors and expand nb_agent dim 
            seq = np.expand_dims( seq[:,0], 1) #B,1,tpred,2 # keep only first neighbors and expand nb_agent dim 
            
            active_last_points = []
            original_x = []

            
            active_mask = (y != self.padding).astype(int)
            active_mask_in = (X != self.padding).astype(int)            

            if self.predict_offsets:

                  if self.predict_offsets == 1 :
                        last_points = np.repeat(  np.expand_dims(X[:,:,-1],2),  self.t_pred, axis=2) #B,1,tpred,2
                  
                  elif self.predict_offsets == 2: # y shifted left
                        last_points = seq[:,:,self.t_obs-1:self.seq_len-1]

                  active_last_points = np.multiply(active_mask,last_points)
                  y = np.subtract(y,active_last_points)

            if self.offsets_input:
                  # concatenate the first point of X to X in order to get as many offsets as position
                  first_points = np.concatenate([np.expand_dims(X[:,:,0],2), X[:,:,0:self.t_obs-1]], axis = 2)

                  # apply active mask of input points
                  active_first_points = np.multiply(active_mask_in,first_points)

                  # keep original inputs
                  original_x = X
                  # apply the input active mask on the original inputs to remove the padding
                  original_x = np.multiply(original_x,active_mask_in) # put padding to 0

                  # subtract x shifted right to x in order to get offsets, offsets[0] = 0
                  X = np.subtract(X,active_first_points)
                  
            y = np.multiply(y,active_mask) # put padding to 0
            X = np.multiply(X,active_mask_in) # put padding to 0


            return X,y,(active_mask_in,active_mask),active_last_points,original_x
