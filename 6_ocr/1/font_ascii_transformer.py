# coding=utf-8
# 参考： https://github.com/Kyubyong/transformer

import tensorflow as tf
import numpy as np
import os
import utils
import time
import random
import cv2
from PIL import Image, ImageDraw, ImageFont
import tensorflow.contrib.slim as slim
import math
import urllib,json,io
import utils_pil, utils_font, utils_nn
import font_ascii_clean
import operator
from collections import deque

curr_dir = os.path.dirname(__file__)

image_height = 32
# image_size = 512
# resize_image_size = 256
# 所有 unicode CJK统一汉字（4E00-9FBB） + ascii的字符加 + ctc blank
# https://zh.wikipedia.org/wiki/Unicode
# https://zh.wikipedia.org/wiki/ASCII
ASCII_CHARS = [chr(c) for c in range(32,126+1)]
#ZH_CHARS = [chr(c) for c in range(int('4E00',16),int('9FBB',16)+1)]
#ZH_CHARS_PUN = ['。','？','！','，','、','；','：','「','」','『','』','‘','’','“','”',\
#                '（','）','〔','〕','【','】','—','…','–','．','《','》','〈','〉']

CHARS = ASCII_CHARS #+ ZH_CHARS + ZH_CHARS_PUN
# CHARS = ASCII_CHARS
CLASSES_NUMBER = len(CHARS) + 1 
SEQ_LENGTH = 250

#初始化学习速率
LEARNING_RATE_INITIAL = 1e-6
# LEARNING_RATE_DECAY_FACTOR = 0.9
# LEARNING_RATE_DECAY_STEPS = 2000
REPORT_STEPS = 500

BATCHES = 50
BATCH_SIZE = 1
TRAIN_SIZE = BATCHES * BATCH_SIZE
TEST_BATCH_SIZE = BATCH_SIZE
POOL_COUNT = 4
POOL_SIZE  = round(math.pow(2,POOL_COUNT))
MODEL_SAVE_NAME = "model_ascii_transformer"

def RES(inputs, lables, reuse = False):
    with tf.variable_scope("OCR", reuse=reuse):
        print("inputs shape:",inputs.shape)
        with tf.variable_scope("RESNET", reuse=reuse):
            layer = utils_nn.resNet50(inputs, True)    # N H/16 W 2048
            print("resNet shape:",layer.shape)

            layer = slim.conv2d(layer, 1024, [1,1], normalizer_fn=slim.batch_norm, activation_fn=None) 
            layer = slim.conv2d(layer, 512, [1,1], normalizer_fn=slim.batch_norm, activation_fn=None) 
            layer = slim.conv2d(layer, 256, [1,1], normalizer_fn=slim.batch_norm, activation_fn=None) 
            # N 1 W//4 256
            layer = slim.avg_pool2d(layer, [2, 4], [2, 4]) 

            shape = tf.shape(layer)
            batch_size, width, channel = shape[0], shape[2], shape[3]
            layer = tf.reshape(layer,(batch_size, width, 256))
            print("resNet_seq shape:",layer.shape)

        with tf.variable_scope("Transformer", reuse=reuse):
            # layer = tf.transpose(layer, (0, 2, 1))  # NWC ==> NCW
            layer = Transformer(layer, 256, width//2, batch_size, width, height)    
            print("Transformer shape:",layer.shape)

        return res_layer,layer

def Transformer(inputs, lables, num_units, num_heads, batch_size, width):
    layer = inputs
    for i in range(6):
        with tf.variable_scope("encoder-%s"%i):
            layer = multihead_attention(layer, lables, num_units, num_heads)
            print("Transformer-attention-%s:"%i, layer.shape)
            layer = feedforward(layer, num_units*4, num_units, batch_size, width)
            print("Transformer-feed-%s:"%i, layer.shape)
    return layer

def feedforward(inputs, f_size, s_size, batch_size, width):
    layer = tf.reshape(inputs, [batch_size, width, 1, s_size]) # N, W, 1, 1024
    _layer = slim.conv2d(layer, f_size,   [1,1], normalizer_fn=slim.batch_norm, activation_fn=tf.nn.leaky_relu)
    _layer = slim.conv2d(_layer,  s_size, [1,1], normalizer_fn=slim.batch_norm, activation_fn=None)
    layer = tf.nn.leaky_relu(_layer + layer)   
    layer = tf.reshape(layer, [batch_size, width, s_size])
    return layer

def multihead_attention(queries, keys, num_units, num_heads):
    # Linear projections    
    Q = slim.fully_connected(queries, num_units, activation_fn=tf.nn.leaky_relu) # (N, W_q, C)
    K = slim.fully_connected(keys, num_units, activation_fn=tf.nn.leaky_relu)    # (N, W_k, C)
    V = slim.fully_connected(keys, num_units, activation_fn=tf.nn.leaky_relu)    # (N, W_k, C)

    # Split and concat
    Q_ = tf.concat(tf.split(Q, num_heads, axis=2), axis=0) # (h*N, W_q, C/h) 
    K_ = tf.concat(tf.split(K, num_heads, axis=2), axis=0) # (h*N, W_k, C/h) 
    V_ = tf.concat(tf.split(V, num_heads, axis=2), axis=0) # (h*N, W_k, C/h) 

    # Multiplication
    # (h*N, W_q, C/h) matmul (h*N, C/h, W_k) ==> (h*N, W_q, W_k)
    outputs = tf.matmul(Q_, tf.transpose(K_, [0, 2, 1])) 

    # Key Masking
    # 取key的最后求和，取绝对值，然后标记 0/1 ，这里基本上应该都不为 1
    # (N, W_k, C) => (N, W_k)
    key_masks = tf.sign(tf.abs(tf.reduce_sum(keys, axis=-1))) 
    # 然后按 h 复制为 => (h*N, W_k)
    key_masks = tf.tile(key_masks, [num_heads, 1]) 
    # 再复制为 (h*N, W_q, W_k)
    key_masks = tf.tile(tf.expand_dims(key_masks, 1), [1, tf.shape(queries)[1], 1]) 
    
    # 按 key_masks 中 为 0 的 位置，替换 outputs 的值为 -4294967295
    # (h*N, W_q, W_k)
    paddings = tf.ones_like(outputs)*(-2**32+1)
    outputs = tf.where(tf.equal(key_masks, 0), paddings, outputs) 
    
    # Activation
    # 取得 (h*N, W_q, W_k) 的影响度, 按 (h*N, W_q) 对 W_k 的影响度
    outputs = tf.nn.softmax(outputs)

    # Query Masking
    # 得到 Query 的 masking 的 C的和为0和不为0
    # (N, W_q) 
    query_masks = tf.sign(tf.abs(tf.reduce_sum(queries, axis=-1))) 
    # (h*N, W_q)
    query_masks = tf.tile(query_masks, [num_heads, 1]) 
    # (h*N, W_q, W_k)
    query_masks = tf.tile(tf.expand_dims(query_masks, -1), [1, 1, tf.shape(keys)[1]]) 
   
    # 删除掉那些加起来为0的项，得到最大影响度
    # (h*N, W_q, W_k) * (h*N, W_q, W_k)
    outputs *= query_masks 

    # Weighted sum
    # (h*N, W_q, W_k) matmul (h*N, W_k, C/h) ==> (h*N, W_q, C/h)
    outputs = tf.matmul(outputs, V_) # ( h*N, T_q, C/h)
    
    # Restore shape
    # (h*N, W_q, C/h) => (N, W_q, C)
    outputs = tf.concat(tf.split(outputs, num_heads, axis=0), axis=2 )  
            
    # 补偿
    outputs += queries
            
    # 归一化
    outputs = slim.batch_norm(outputs) # (N, T_q, C)
    return outputs


def neural_networks():
    # 输入：训练的数量，一张图片的宽度，一张图片的高度 [-1,-1,16]
    inputs = tf.placeholder(tf.float32, [None, image_height, None, 1], name="inputs")
    labels_sparse = tf.sparse_placeholder(tf.int32, name="labels_sparse")
    labels_fix = tf.placeholder(tf.int32, name="labels_fix")
    seq_len = tf.placeholder(tf.int32, [None], name="seq_len")
    global_step = tf.Variable(0, trainable=False)
    lr = tf.Variable(LEARNING_RATE_INITIAL, trainable=False)

    res_layer, net_res = RES(inputs, labels_fix, reuse = False)
    res_vars  = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='OCR')

    # 需要变换到 time_major == True [max_time x batch_size x 2048]
    net_res = tf.transpose(net_res, (1, 0, 2))
    res_loss = tf.reduce_mean(tf.nn.ctc_loss(labels=labels, inputs=net_res, sequence_length=seq_len))
    # res_optim = tf.train.AdamOptimizer(LEARNING_RATE_INITIAL).minimize(res_loss, global_step=global_step, var_list=res_vars)
 
    # 防止梯度爆炸
    res_optim = tf.train.AdamOptimizer(lr)
    with tf.name_scope("ClipGradients"):
        gvs = res_optim.compute_gradients(res_loss)
        capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in gvs]
    res_optim = res_optim.apply_gradients(capped_gvs, global_step=global_step)

    res_decoded, _ = tf.nn.ctc_beam_search_decoder(net_res, seq_len, beam_width=10, merge_repeated=False)
    res_acc = tf.reduce_sum(tf.edit_distance(tf.cast(res_decoded[0], tf.int32), labels, normalize=False))
    res_acc = 1 - res_acc / tf.to_float(tf.size(labels.values))


    # 加入日志
    tf.summary.scalar('res_loss', res_loss)
    tf.summary.scalar('res_acc', res_acc)
    # res_images = res_layer[-1]
    # res_images = tf.transpose(res_images, perm=[2, 0, 1])
    # tf.summary.image('net_res', tf.expand_dims(res_images,-1), max_outputs=9)
    for var in res_vars:
        tf.summary.histogram(var.name, var)
    summary = tf.summary.merge_all()

    return  inputs, labels, global_step, lr, summary, \
            res_loss, res_optim, seq_len, res_acc, res_decoded


ENGFontNames, CHIFontNames = utils_font.get_font_names_from_url()
print("EngFontNames", ENGFontNames)
print("CHIFontNames", CHIFontNames)
AllFontNames = ENGFontNames + CHIFontNames
AllFontNames.remove("方正兰亭超细黑简体")
AllFontNames.remove("幼圆")
AllFontNames.remove("方正舒体")
AllFontNames.remove("方正姚体")
AllFontNames.remove("华文新魏")
AllFontNames.remove("Impact")
AllFontNames.remove("Gabriola")

eng_world_list = open(os.path.join(curr_dir,"eng.wordlist.txt"),encoding="UTF-8").readlines() 

def list_to_chars(list):
    try:
        return "".join([CHARS[v] for v in list])
    except Exception as err:
        return "Error: %s" % err        

if os.path.exists(os.path.join(curr_dir,"train.txt")):
    train_text_lines = open(os.path.join(curr_dir,"train.txt")).readlines()
else:
    train_text_lines = []


def train():
    inputs, labels, global_step, lr, summary, \
        res_loss, res_optim, seq_len, res_acc, res_decoded = neural_networks()

    curr_dir = os.path.dirname(__file__)
    model_dir = os.path.join(curr_dir, MODEL_SAVE_NAME)
    if not os.path.exists(model_dir): os.mkdir(model_dir)
    model_R_dir = os.path.join(model_dir, "RL32")
    if not os.path.exists(model_R_dir): os.mkdir(model_R_dir)

    log_dir = os.path.join(model_dir, "logs")
    if not os.path.exists(log_dir): os.mkdir(log_dir)

    init = tf.global_variables_initializer()
    with tf.Session() as session:
        session.run(init)

        r_saver = tf.train.Saver(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='OCR'), sharded=True, max_to_keep=5)

        for i in range(3):
            ckpt = tf.train.get_checkpoint_state(model_R_dir)
            if ckpt and ckpt.model_checkpoint_path:
                print("Restore Model OCR...")
                stem = os.path.basename(ckpt.model_checkpoint_path)
                restore_iter = int(stem.split('-')[-1])
                try:
                    r_saver.restore(session, ckpt.model_checkpoint_path)    
                except:
                    new_restore_iter = restore_iter - BATCHES
                    with open(os.path.join(model_R_dir,"checkpoint"),'w') as f:
                        f.write('model_checkpoint_path: "OCR.ckpt-%s"\n'%new_restore_iter)
                        f.write('all_model_checkpoint_paths: "OCR.ckpt-%s"\n'%new_restore_iter)
                    continue
                session.run(global_step.assign(restore_iter))
                print("Restored to %s."%restore_iter)
                break
            else:
                break
            print("restored fail, return")
            return

        train_writer = tf.summary.FileWriter(log_dir, session.graph)

        AllLosts={}
        accs = deque(maxlen=100)
        losts = deque(maxlen=200)
        while True:
            errR = 1
            batch_size = BATCH_SIZE
            for batch in range(BATCHES):
                train_inputs, train_labels, _, _, train_info =  font_dataset.get_next_batch_for_res(batch_size,
                    has_sparse=False, has_onehot=False, max_width=4096, height=32, need_pad_width_to_max_width=True)

                train_labels_fix = np.ones((batch_size, SEQ_LENGTH))
                train_labels_fix *= (CLASSES_NUMBER-1)
                for i in range(batch_size):
                    np.put(train_labels_fix[i],np.arange(len(train_labels[i])),train_labels[i])
                train_seq_len = np.ones(batch_size) * SEQ_LENGTH

                start = time.time()                
                feed = {inputs: train_inputs, labels: train_labels, seq_len: train_seq_len} 

                # print("train...")
                errR, acc, _ , steps= session.run([res_loss, res_acc, res_optim, global_step], feed)
                # print("trained.")
                font_length = int(train_info[0][-1])
                font_info = train_info[0][0]+"/"+train_info[0][1]+"/"+str(font_length)
                accs.append(acc)
                avg_acc = sum(accs)/len(accs)

                losts.append(errR)
                avg_losts = sum(losts)/len(losts)

                # errR = errR / font_length
                print("%s, %d time: %4.4fs, acc: %.4f, avg_acc: %.4f, loss: %.4f, avg_loss: %.4f, info: %s " % \
                    (time.ctime(), steps, time.time() - start, acc, avg_acc, errR, avg_losts, font_info))

                # 如果当前lost低于平均lost，就多训练
                if errR/avg_losts > 1.5 or acc/avg_acc < 0.5:
                # for _ in range(int(errR//avg_losts)):
                    errR, acc, _ = session.run([res_loss, res_acc, res_optim], feed)
                    session.run(global_step.assign(steps))
                    accs.append(acc)
                    avg_acc = sum(accs)/len(accs)                  
                    print("%s, %d time: %4.4fs, acc: %.4f, avg_acc: %.4f, loss: %.4f, avg_loss: %.4f, info: %s " % \
                        (time.ctime(), steps, time.time() - start, acc, avg_acc, errR, avg_losts, font_info))
                    if acc/avg_acc < 0.5:
                        decoded_list = session.run(res_decoded[0], feed)
                        report(train_labels, decoded_list)

                if steps<5000:        
                    session.run(tf.assign(lr, 1e-4))
                elif steps<50000:            
                    session.run(tf.assign(lr, 1e-5))
                else:
                    session.run(tf.assign(lr, 1e-6))


                # if np.isnan(errR) or np.isinf(errR) :
                #     print("Error: cost is nan or inf")
                #     return

                for info in train_info:
                    key = ",".join(info)
                    if key in AllLosts:
                        AllLosts[key]=AllLosts[key]*0.99+acc*0.01
                    else:
                        AllLosts[key]=acc

                if acc/avg_acc<=0.8:
                    for i in range(batch_size): 
                        filename = "%s_%s_%s_%s_%s_%s_%s.png"%(acc, steps, i, \
                            train_info[i][0], train_info[i][1], train_info[i][2], train_info[i][3])
                        cv2.imwrite(os.path.join(curr_dir,"test",filename), train_inputs[i] * 255)                    
                # 报告
                if steps >0 and steps % REPORT_STEPS == 0:
                    train_inputs, train_labels, train_seq_len, train_info = get_next_batch_for_res(batch_size)   
           
                    decoded_list = session.run(res_decoded[0], {inputs: train_inputs, seq_len: train_seq_len}) 

                    for i in range(batch_size): 
                        cv2.imwrite(os.path.join(curr_dir,"test","%s_%s.png"%(steps,i)), train_inputs[i] * 255) 
                        
                    report(train_labels, decoded_list)
                    
                    sorted_fonts = sorted(AllLosts.items(), key=operator.itemgetter(1), reverse=False)
                    for f in sorted_fonts[:20]:
                        print(f)
                        
            # 如果当前 loss 为 nan，就先不要保存这个模型
            if np.isnan(errR) or np.isinf(errR):
                continue
            print("Save Model OCR ...")
            r_saver.save(session, os.path.join(model_R_dir, "OCR.ckpt"), global_step=steps)         
            # 保存日志
            logs = session.run(summary, feed)
            train_writer.add_summary(logs, steps)


def report(train_labels, decoded_list):
    original_list = utils.decode_sparse_tensor(train_labels)
    detected_list = utils.decode_sparse_tensor(decoded_list)
    if len(original_list) != len(detected_list):
        print("len(original_list)", len(original_list), "len(detected_list)", len(detected_list),
            " test and detect length desn't match")
    acc = 0.
    for idx in range(min(len(original_list),len(detected_list))):
        number = original_list[idx]
        detect_number = detected_list[idx]  
        hit = (number == detect_number)
        print("----------",hit,"------------")          
        print(list_to_chars(number), "(", len(number), ")")
        print(list_to_chars(detect_number), "(", len(detect_number), ")")
        # 计算莱文斯坦比
        import Levenshtein
        acc += Levenshtein.ratio(list_to_chars(number),list_to_chars(detect_number))
    print("Test Accuracy:", acc / len(original_list))


if __name__ == '__main__':
    train()