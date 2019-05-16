import numpy as np
import torch
import json
import argparse
from torchvision.utils import make_grid
# from base import BaseTrainer
import torch.nn.functional as F
# from utils import Logger
import data_loader.data_loaders as module_data
import model.metric as module_metric
import model.loss as module_loss
import model.model as module_arch
from train import get_instance
import logging
import os
from torchvision.utils import make_grid, save_image
# from utils.util import (convert_to_grayscale,
#                             save_gradient_images,
#                             get_positive_negative_saliency)


class GuidedBackprop():
    def __init__(self, model, loss, content_loss, metrics, resume, config):
        self.config = config    # get configuration file
        self.logger = logging.getLogger(self.__class__.__name__) # get log
        self.device, device_ids = self._prepare_device(0) # get gpu devices
        # get model, loss, metrics, content_loss stuff
        self.loss = loss
        self.metrics = metrics
        # self.data_loader = data_loader
        # put necessary part to GPU or CPU? based on what device you get available
        self.model = model.to(self.device)
        self.content_loss = content_loss.to(self.device)
        if len(device_ids) > 1:
            self.model = torch.nn.DataParallel(model, device_ids=device_ids)

        # parameters
        self.alpha_loss_weight = self.config['alpha_loss_weight']
        self.comp_loss_weight = self.config['comp_loss_weight']
        self.content_loss_weight = self.config['content_loss_weight']
        
        # variables to store hooked gradients
        self.gradients_ms = None
        self.gradients_fused = None
        self.hook_layers()
        self._resume_checkpoint(resume)


    def hook_layers(self):
        def hook_ms_function(module, grad_in, grad_out):
            self.gradients_ms = grad_out[0]
        def hook_gradients_fused(module, grad_in, grad_out):
            self.gradients_fused = grad_out[0]

        ms_last_layer = list(self.model.msmnet_model.output._modules.items())[0][1][-1]
        fused_last_layer = self.model.fusion_model._modules['fusion_model'][-1][-1]

        ms_last_layer.register_backward_hook(hook_ms_function)
        fused_last_layer.register_backward_hook(hook_gradients_fused)

    def generate_gradients(self, input_img, gt):
        """
        Training logic for an epoch

        :param epoch: Current training epoch.
        :return: A log that contains all information you want to save.

        Note:
            If you have additional information to record, for example:
                > additional_log = {"x": x, "y": y}
            merge it with log before return. i.e.
                > log = {**log, **additional_log}
                > return log

            The metrics in log must have the key 'metrics'.
        """
        self.model.eval()

        img_scale1 = input_img.to(self.device)
        img_scale2 = F.interpolate(img_scale1.clone(), scale_factor=0.5)
        img_scale3 = F.interpolate(img_scale1.clone(), scale_factor=0.25)

        gt = gt.to(self.device)
        self.model.zero_grad()

        output, ms_output, at_output = self.model(img_scale1, img_scale2, img_scale3)
        ## content loss
        # pred_object = img_scale1 * output
        # gt_object = img_scale1 * gt
        # content_loss_ = self.content_loss(pred_object, gt_object) *\
        #                 self.content_loss_weight
        ## comp loss
        # fg = img_scale1 * gt
        # bg = img_scale1 * (1-gt)
        # color_pred = output * fg + (1-output) * bg
        # comp_loss_ = self.loss(color_pred, img_scale1) * self.comp_loss_weight

        ## overall loss
        alpha_loss_ = self.loss(output, gt)# * self.alpha_loss_weight
        
        loss = alpha_loss_ #+ comp_loss_ + content_loss_
        
        ## backprop
        loss.backward()
        gradient_ms_arr = self.gradients_ms.data[0].unsqueeze(1)
        # gradient_at_arr = self.gradients_at.data[0].unsqueeze(1)
        gradient_fused_arr = self.gradients_fused[0].unsqueeze(1)
        forward_ms_arr = ms_output[0].unsqueeze(1)
        forward_at_arr = at_output[0].unsqueeze(1)

        return [gradient_ms_arr, gradient_fused_arr, forward_ms_arr, forward_at_arr]


    def _resume_checkpoint(self, resume_path, finetune=False):
        """
        Resume from saved checkpoints

        :param resume_path: Checkpoint path to be resumed
        """
        self.logger.info("Loading checkpoint: {} ...".format(resume_path))
        checkpoint = torch.load(resume_path)
        if not finetune:
            self.start_epoch = checkpoint['epoch'] + 1
            self.mnt_best = checkpoint['monitor_best']

        # load architecture params from checkpoint.
        if checkpoint['config']['arch'] != self.config['arch']:
            self.logger.warning('Warning: Architecture configuration given in config file is different from that of checkpoint. ' + \
                                'This may yield an exception while state_dict is being loaded.')
        model_dict = self.model.state_dict()
        new_dict = {k: v for k, v in checkpoint['state_dict'].items() if k in model_dict.keys()}
        model_dict.update(new_dict)
        self.model.load_state_dict(model_dict)

        self.logger.info("Checkpoint '{}' (epoch {}) loaded".format(resume_path, self.start_epoch))
        

    def _prepare_device(self, n_gpu_use):
        """ 
        setup GPU device if available, move model into configured device
        """ 
        n_gpu = torch.cuda.device_count()
        if n_gpu_use > 0 and n_gpu == 0:
            self.logger.warning("Warning: There\'s no GPU available on this machine, training will be performed on CPU.")
            n_gpu_use = 0
        if n_gpu_use > n_gpu:
            self.logger.warning("Warning: The number of GPU\'s configured to use is {}, but only {} are available on this machine.".format(n_gpu_use, n_gpu))
            n_gpu_use = n_gpu
        device = torch.device('cuda:0' if n_gpu_use > 0 else 'cpu')
        list_ids = list(range(n_gpu_use))
        return device, list_ids

def save_gradient_images(gradient, file_name):
    """
        Exports the original gradient image

    Args:
        gradient (np arr): Numpy array of the gradient with shape (3, 224, 224)
        file_name (str): File name to be exported
    """
    if not os.path.exists('results'):
        os.makedirs('results')
    # Normalize
    gradient = gradient - gradient.min()
    gradient /= gradient.max()
    # # Save image
    path_to_file = os.path.join('results', file_name + '.png')
    save_image(gradient, path_to_file)


def main(config, resume=None):
    # train_logger = Logger()
    data_loader = get_instance(module_data, 'adobe_data_loader', config)
    model = get_instance(module_arch, 'arch', config)
    loss = getattr(module_loss, config['loss'])
    content_loss = get_instance(module_loss, 'content_loss', config)
    metrics = [getattr(module_metric, met) for met in config['metrics']]

    BGP = GuidedBackprop(model, loss, content_loss, metrics, resume, config)

    dl_iter = iter(data_loader)
    sample = next(dl_iter)
    img_scale1 = sample['image']
    gt = sample['gt']
    name = sample['name'][0]

    gradient_list_ = BGP.generate_gradients(img_scale1, gt)
    base_fn = os.path.splitext(os.path.basename(name))[0]
    grid_ms = make_grid(gradient_list_[0], nrow=8, normalize=True)
    grid_fused = make_grid(gradient_list_[1], nrow=8, normalize=True)
    grid_forward_ms = make_grid(gradient_list_[2], nrow=8, normalize=True)
    grid_forward_at = make_grid(gradient_list_[3], nrow=8, normalize=True)
    save_gradient_images(grid_ms, base_fn+'ms')
    save_gradient_images(grid_fused, base_fn+'fused')
    save_image(grid_forward_ms, \
        os.path.join('results', base_fn+'forward_ms.png'))
    save_image(grid_forward_at, \
        os.path.join('results', base_fn+'forward_at.png'))

    print('Gradient saved')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch Template')
    
    parser.add_argument('-c', '--config', default=None, type=str,
                           help='config file path (default: None)')
    parser.add_argument('-r', '--resume', default=None, type=str,
                           help='path to latest checkpoint (default: None)')
    args = parser.parse_args()

    if args.config:
        # load config file
        config = json.load(open(args.config))
    else:
        raise AssertionError("Configuration file need to be specified. Add '-c config.json', for example.")

    if args.resume is None:
        raise AssertionError("Please specify a resume pth.")

    main(config, args.resume)