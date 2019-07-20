# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable


class polynomial():
    """
    Polynomial class with exact integral calculation according to the
    trapezium rule.
    """
    def __init__(self, coeffs, a=0, b=0.7, n=100):
        self.a1, self.b1, self.c1 = torch.chunk(coeffs, 3, 1)
        self.a1, self.b1, self.c1 = self.a1.squeeze(), self.b1.squeeze(), self.c1.squeeze()
        self.a = a
        self.b = b
        self.n = n

    def calc_pol(self, x):
        return self.a1*x**2 + self.b1*x + self.c1

    def trapezoidal(self, other):
        h = float(self.b - self.a) / self.n
        s = 0.0
        s += abs(self.calc_pol(self.a)/2.0 - other.calc_pol(other.a)/2.0)
        for i in range(1, self.n):
            s += abs(self.calc_pol(self.a + i*h) - other.calc_pol(self.a + i*h))
        s += abs(self.calc_pol(self.b)/2.0 - other.calc_pol(self.b)/2.0)
        out = s*h
        return out


# pol1 = polynomial(coeffs=torch.FloatTensor([[0, 1, 0]]), a=-1, b=1)
# pol2 = polynomial(coeffs=torch.FloatTensor([[0, 0, 0]]), a=-1, b=1)
# pol1 = polynomial(coeffs=torch.FloatTensor([[0, 1, 0]]), a=0, b=1)
# pol2 = polynomial(coeffs=torch.FloatTensor([[1, 0, 0]]), a=0, b=1)
# print('Area by trapezium rule is {}'.format(pol1.trapezoidal(pol2)))


def define_loss_crit(options):
    '''
    Define loss cirterium:
        -MSE loss on curve parameters in ortho view
        -MSE loss on points after backprojection to normal view
        -Area loss
    '''
    if options.loss_policy == 'mse':
        loss_crit = MSE_Loss(options)
    elif options.loss_policy == 'homography_mse':
        loss_crit = Homography_MSE_Loss(options)
    elif options.loss_policy == 'area':
        loss_crit = Area_Loss(options.order, options.weight_funct)
    else:
        return NotImplementedError('The requested loss criterion is not implemented')
    return loss_crit, CrossEntropyLoss2d(options.weight_seg, seg=True)


class CrossEntropyLoss2d(nn.Module):
    '''
    Standard 2d cross entropy loss on all pixels of image
    My implemetation (but since Pytorch 0.2.0 libs have their
    owm optimized implementation, consider using theirs)
    '''
    def __init__(self, weight=None, size_average=True, seg=False):
        if seg:
            weights = torch.Tensor([1] + [weight]*(2))
            weights = weights.cuda()
        super(CrossEntropyLoss2d, self).__init__()
        self.nll_loss = nn.NLLLoss2d(weights, size_average)

    def forward(self, inputs, targets):
        return self.nll_loss(F.log_softmax(inputs, dim=1), targets[:, 0, :, :])


class Area_Loss(nn.Module):
    '''
    Compute area between curves by integrating (x1 - x2)^2 over y
    *Area:
        *order 0: int((c1 - c2)**2)dy
        *order 1: int((b1*y - b2*y + c1 - c2)**2)dy
        *order 2: int((a1*y**2 - a2*y**2 + b1*y - b2*y + c1 - c2)**2)dy

    *A weight function W can be added:
        Weighted area: int(W(y)*diff**2)dy
        with W(y):
            *1
            *(1-y)
            *(1-y**0.5)
    '''
    def __init__(self, order, weight_funct):
        super(Area_Loss, self).__init__()
        self.order = order
        self.weight_funct = weight_funct

    def forward(self, params, gt_params, compute=True):
        diff = params.squeeze(-1) - gt_params
        a = diff[:, 0]
        b = diff[:, 1]
        t = 0.7 # up to which y location to integrate
        if self.order == 2:
            c = diff[:, 2]
            if self.weight_funct == 'none':
                # weight (1)
                loss_fit = (a**2)*(t**5)/5+2*a*b*(t**4)/4 + \
                           (b**2+c*2*a)*(t**3)/3+2*b*c*(t**2)/2+(c**2)*t
            elif self.weight_funct == 'linear':
                # weight (1-y)
                loss_fit = c**2*t - t**5*((2*a*b)/5 - a**2/5) + \
                           t**2*(b*c - c**2/2) - (a**2*t**6)/6 - \
                           t**4*(b**2/4 - (a*b)/2 + (a*c)/2) + \
                           t**3*(b**2/3 - (2*c*b)/3 + (2*a*c)/3)
            elif self.weight_funct == 'quadratic':
                # weight (1-y**0.5)
                loss_fit = t**3*(1/3*b**2 + 2/3*a*c) - \
                           t**(7/2)*(2/7*b**2 + 4/7*a*c) + \
                           c**2*t + 0.2*a**2*t**5 - 2/11*a**2*t**(11/2) - \
                           2/3*c**2*t**(3/2) + 0.5*a*b*t**4 - \
                           4/9*a*b*t**(9/2) + b*c*t**2 - 0.8*b*c*t**(5/2)
            else:
                return NotImplementedError('The requested weight function is \
                        not implemented, only order 1 or order 2 possible')
        elif self.order == 1:
            loss_fit = (b**2)*t + a*b*(t**2) + ((a**2)*(t**3))/3
        else:
            return NotImplementedError('The requested order is not implemented, only none, linear or quadratic possible')

        # Mask select if lane is present
        mask = torch.prod(gt_params != 0, 1).byte()
        loss_fit = torch.masked_select(loss_fit, mask)
        loss_fit = loss_fit.mean(0) if loss_fit.size()[0] != 0 else 0 # mean over the batch
        return loss_fit


class MSE_Loss(nn.Module):
    '''
    Compute mean square error loss on curve parameters
    in ortho or normal view
    '''
    def __init__(self, options):
        super(MSE_Loss, self).__init__()
        self.loss_crit = nn.MSELoss()
        if not options.no_cuda:
            self.loss_crit = self.loss_crit.cuda()

    def forward(self, params, gt_params, compute=True):
        loss = self.loss_crit(params.squeeze(-1), gt_params)
        return loss


class Homography_MSE_Loss(nn.Module):
    '''
    Compute mean square error loss on points in normal view
    instead of parameters in ortho view
    '''
    def __init__(self, options):
        super(Homography_MSE_Loss, self).__init__()
        start = 250
        delta = 10
        num_heights = (720-start)//delta
        self.y_d = Variable((torch.arange(start, 720, delta)-80)/(639))
        self.y_orig = self.y_d.expand(options.batch_size, num_heights)
        self.ones = Variable(torch.ones(options.batch_size, num_heights))

        if options.cuda:
            self.y_d = self.y_d.cuda()
            self.y_orig = self.y_orig.cuda()
            self.ones = self.ones.cuda()

    def forward(self, params, gt_params, M, M_inv):
        y_prime = (M[:,1,1:2]*self.y_d + M[:,1,2:])/(M[:,2,1:2]*self.y_d+M[:,2,2:])
        y_eval = 1 - y_prime
        Y = torch.stack((y_eval**2, y_eval, self.ones),1)
        x_prime=torch.bmm(params.transpose(1, 2), Y).squeeze(1)
        coordinates = torch.stack((x_prime, y_prime, self.ones),1)
        trans = torch.bmm(M_inv, coordinates)

        x_cal = trans[:,0,:]/trans[:,2,:]
#        y_cal = trans[:,1,:]/trans[:,2,:]
        y_orig_eval = 1-self.y_orig
        Y = torch.stack((y_orig_eval**2, y_orig_eval, self.ones),1)
        x = torch.bmm(gt_params.unsqueeze(1), Y).squeeze(1)
        loss = torch.mean(torch.sum(((x-x_cal)**2),1))
        return loss, x, x_cal


class Laneline_3D_loss(nn.Module):
    """
    compute the loss between predicted 3D lanelines in anchor representation,
    and the ground-truth 3D lanelines in anchor representation.
    loss = loss1 + loss2 + loss2
    loss1: cross entropy loss for lane type classification
    loss2: sum of geometric distance betwen 3D lane anchor points in X and Z offsets
    loss3: error in estimating pitch and camera heights
    """
    def __init__(self):
        super(Laneline_3D_loss, self).__init__()

    def forward(self, pred_3D_lanes, gt_3D_lanes, pred_pitch, gt_pitch, pred_hcam, gt_hcam):
        """

        :param pred_3D_lanes: predicted tensor with size N x (ipm_w/8) x 3*(2*K+1)
        :param gt_3D_lanes: ground-truth tensor with size N x (ipm_w/8) x 3*(2*K+1)
        :param pred_pitch: predicted pitch with size N
        :param gt_pitch: ground-truth pitch with size N
        :param pred_hcam: predicted camera height with size N
        :param gt_hcam: ground-truth camera height with size N
        :return:
        """
        sizes = pred_3D_lanes.shape
        # reshape to N x ipm_w/8 x 3 x (2K+1)
        pred_3D_lanes = pred_3D_lanes.reshape(sizes[0], sizes[1], 3, -1)
        gt_3D_lanes = gt_3D_lanes.reshape(sizes[0], sizes[1], 3, -1)

        # class prob N x ipm_w/8 x 3 x 1, anchor value N x ipm_w/8 x 3 x 2K
        pred_class = pred_3D_lanes[:, :, :, -1].unsqueeze(-1)
        pred_anchors = pred_3D_lanes[:, :, :, :-1]
        gt_class = gt_3D_lanes[:, :, :, -1].unsqueeze(-1)
        gt_anchors = gt_3D_lanes[:, :, :, :-1]

        # pred_class need to apply softmax to convert to (0,1) range, or the log of it will cause error
        # apply to gt_class for safe
        pred_class = F.softmax(pred_class, dim=2)
        gt_class = F.softmax(gt_class, dim=2)
        loss1 = -torch.sum(gt_class*torch.log(pred_class) +
                           (torch.ones_like(gt_class)-gt_class)*torch.log((torch.ones_like(pred_class)-pred_class)))
        # applying L1 norm does not need to separate X and Z
        loss2 = torch.sum(torch.norm(gt_class*(pred_anchors-gt_anchors), p=1, dim=3))
        loss3 = torch.sum(torch.abs(gt_pitch-pred_pitch))+torch.sum(torch.abs(gt_hcam-pred_hcam))
        return loss1+loss2+loss3


# unit test
if __name__ == '__main__':
    criterion = Laneline_3D_loss()
    criterion = criterion.cuda()

    pred_3D_lanes = torch.rand(8, 26, 39).cuda()
    gt_3D_lanes = torch.rand(8, 26, 39).cuda()
    pred_pitch = torch.ones(8).float().cuda()
    gt_pitch = torch.ones(8).float().cuda()
    pred_hcam = torch.ones(8).float().cuda()
    gt_hcam = torch.ones(8).float().cuda()

    loss = criterion(pred_3D_lanes, gt_3D_lanes, pred_pitch, gt_pitch, pred_hcam, gt_hcam)

    print(loss)
