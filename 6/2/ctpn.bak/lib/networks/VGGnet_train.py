# -*- coding:utf-8 -*-
import tensorflow as tf
import numpy as np
from .network import Network
from ..fast_rcnn.config import cfg

class VGGnet_train(Network):
    def __init__(self, trainable=True):
        self.inputs = []
        # 图片数据
        self.data = tf.placeholder(tf.float32, shape=[None, None, None, 3], name='data')
        # 图片的高宽和缩放比例
        self.im_info = tf.placeholder(tf.float32, shape=[None, 3], name='im_info')
        # 图片中的框box，前4位是box的坐标，最后一位是box的类别
        self.gt_boxes = tf.placeholder(tf.float32, shape=[None, 5], name='gt_boxes')
        
        self.gt_ishard = tf.placeholder(tf.int32, shape=[None], name='gt_ishard')
        self.dontcare_areas = tf.placeholder(tf.float32, shape=[None, 4], name='dontcare_areas')
        self.keep_prob = tf.placeholder(tf.float32)
        self.layers = dict({'data':self.data, 'im_info':self.im_info, 'gt_boxes':self.gt_boxes,\
                            'gt_ishard': self.gt_ishard, 'dontcare_areas': self.dontcare_areas})
        self.trainable = trainable
        self.setup()

    def setup(self):
        # n_classes = 21
        # 这里修改为 2 ，文字或背景
        n_classes = cfg.NCLASSES
        # anchor_scales = [8, 16, 32]
        # 这里修改为 [16]
        anchor_scales = cfg.ANCHOR_SCALES
        _feat_stride = [16, ]

        # VGG16的网络结构
        (self.feed('data')
             .conv(3, 3, 64, 1, 1, name='conv1_1')
             .conv(3, 3, 64, 1, 1, name='conv1_2')
             .max_pool(2, 2, 2, 2, padding='VALID', name='pool1')
             .conv(3, 3, 128, 1, 1, name='conv2_1')
             .conv(3, 3, 128, 1, 1, name='conv2_2')
             .max_pool(2, 2, 2, 2, padding='VALID', name='pool2')
             .conv(3, 3, 256, 1, 1, name='conv3_1')
             .conv(3, 3, 256, 1, 1, name='conv3_2')
             .conv(3, 3, 256, 1, 1, name='conv3_3')
             .max_pool(2, 2, 2, 2, padding='VALID', name='pool3')
             .conv(3, 3, 512, 1, 1, name='conv4_1')
             .conv(3, 3, 512, 1, 1, name='conv4_2')
             .conv(3, 3, 512, 1, 1, name='conv4_3')
             .max_pool(2, 2, 2, 2, padding='VALID', name='pool4')
             .conv(3, 3, 512, 1, 1, name='conv5_1')
             .conv(3, 3, 512, 1, 1, name='conv5_2')
             .conv(3, 3, 512, 1, 1, name='conv5_3'))
        # 输出 shape， 这个是按原图说的 [N, H/16, W/16, 512]
        # 后续按 [N, H, W, 512] 备注

        #========= RPN ============
        # 3x3的窗口锚点
        (self.feed('conv5_3')
             .conv(3,3,512,1,1,name='rpn_conv/3x3'))

        # 引入了 bilstm 模型
        # 按 [N * H, W, C] ==> bilstm (128单元) ==> [N * H * W, 2 * 128] ==> FC(512) ==> [N, H, W, 512]
        (self.feed('rpn_conv/3x3').Bilstm(512,128,512,name='lstm_o'))
        # bbox 位置偏移
        # [N, H, W, 512] ==> FC(40) ==> [N, H, W, 10 * 4] 每个坐标取10个框
        (self.feed('lstm_o').lstm_fc(512,len(anchor_scales) * 10 * 4, name='rpn_bbox_pred'))
        # 分类
        # [N, H, W, 512] ==> FC(20) ==> [N, H, W, 10 * 2] 每个坐标取10个框
        (self.feed('lstm_o').lstm_fc(512,len(anchor_scales) * 10 * 2,name='rpn_cls_score'))

        # generating training labels on the fly
        # output: rpn_labels(HxWxA, 2) rpn_bbox_targets(HxWxA, 4) rpn_bbox_inside_weights rpn_bbox_outside_weights
        # 给每个anchor上标签，并计算真值（也是delta的形式），以及内部权重和外部权重
        (self.feed('rpn_cls_score', 'gt_boxes', 'gt_ishard', 'dontcare_areas', 'im_info')
             .anchor_target_layer(_feat_stride, anchor_scales, name = 'rpn-data' ))

        # shape is (1, H, W, Ax2) -> (1, H, WxA, 2)
        # 给之前得到的score进行softmax，得到0-1之间的得分
        (self.feed('rpn_cls_score')
             .spatial_reshape_layer(2, name = 'rpn_cls_score_reshape')
             .spatial_softmax(name='rpn_cls_prob'))