#modeling.py
import json
import os
import pickle
import random
from collections import defaultdict

import gensim
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from gensim import corpora, matutils, models, similarities
from gensim.corpora import Dictionary
from gensim.models import Phrases
from gensim.models.coherencemodel import CoherenceModel
from gensim.test.utils import common_corpus, common_dictionary
from gensim.utils import deaccent, simple_preprocess
from nltk.corpus import stopwords
from nltk.stem.wordnet import WordNetLemmatizer
from nltk.tokenize import word_tokenize
from sklearn.metrics import accuracy_score, f1_score
from smart_open import open
from torch.autograd import Variable
from torchtext import data
from torchtext.vocab import Vectors
from tqdm import tqdm

from utils import *

dtype = torch.FloatTensor
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

def setup_seed(seed):
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed) #cpu
    torch.cuda.manual_seed_all(seed)  #并行gpu
    torch.backends.cudnn.deterministic = True  #cpu/gpu结果一致
    torch.backends.cudnn.benchmark = True

def read_corpus(fname, tokens_only=True, labeled=True):
    with open(fname, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if labeled:
                line = line[2:]
            tokens = gensim.utils.simple_preprocess(line)
            if tokens_only:
                yield tokens
            else:
                # For training data, add tags
                yield gensim.models.doc2vec.TaggedDocument(tokens, [i])

def read_labeled(fname):
    examples = []
    with open(fname, encoding="utf-8") as f:
        for line in f:
            if line == '\n':
                continue
            tokens = gensim.utils.simple_preprocess(line[2:], deacc=True)
            examples.append((tokens, int(line[0])))
    random.shuffle(examples)
    return [x[0] for x in examples], [x[1] for x in examples]

def batch_iter(x, y, batch_size=64, shuffle=True):
    """return batches"""
    data_len = len(x)
    num_batch = int((data_len - 1) / batch_size) + 1

    x_shuffle = x
    y_shuffle = y
    if shuffle:
        examples = list(zip(x, y))
        random.shuffle(examples)
        # indices = np.random.permutation(np.arange(data_len))
        # x_shuffle = x[indices]
        # y_shuffle = y[indices]
        x_shuffle = [x[0] for x in examples]
        y_shuffle = [x[1] for x in examples]

    for i in range(num_batch):
        start_id = i * batch_size
        end_id = min((i + 1) * batch_size, data_len)
        yield torch.LongTensor(x_shuffle[start_id:end_id]), torch.LongTensor(
            y_shuffle[start_id:end_id])

def batch_iter2(biterms, embeddings, vocab_size, batch_size=4096, shuffle=True):
    '''
        biterms: list of tuple [(w1,w2)], w1 and w2 are indexes.
        embeddings: matrix of word embeddings, dtype=torch.Tensor
    '''
    size = embeddings.size()
    data_len = len(biterms)
    num_batch = int((data_len - 1) / batch_size) + 1
    copy = biterms.copy()
    if shuffle:
        random.shuffle(copy)

    for i in range(num_batch):
        start_id = i * batch_size
        end_id = min((i + 1) * batch_size, data_len)
        batch_length = end_id - start_id
        idx_batch = copy[start_id:end_id]
        emb_batch = torch.zeros(batch_length, size[1])
        idx_matrix = np.zeros((batch_length, vocab_size))
        for j in range(len(idx_batch)):
            w1, w2 = idx_batch[j]
            emb_batch[j] = embeddings[w1] + embeddings[w2]
            idx_matrix[j][w1] = 1.
            idx_matrix[j][w2] = 1.
        yield torch.Tensor(idx_matrix).type(torch.bool), emb_batch

def doc_padding(tokens, sen_length, padding_with='<pad>'):
    '''pad a list to a fixed length with certain word'''
    if len(tokens) >= sen_length:
        return tokens[:sen_length]
    else:
        return tokens + [padding_with for i in range(sen_length - len(tokens))]

def evaluate_su_model(model, iterator):
    ''' Evaluate supervised model
    batch[0]: data, batch[1]: label
    '''
    model.eval()
    all_preds = []
    all_y = []
    for idx, batch in enumerate(iterator):
        x = batch[0].to(device)
        y_pred = model(x)
        predicted = torch.max(y_pred.cpu().data, 1)[1]
        all_preds.extend(predicted.numpy())
        all_y.extend(batch[1].numpy())
    accuracy = accuracy_score(all_y, np.array(all_preds).flatten())
    f1 = f1_score(all_y, np.array(all_preds).flatten())
    return accuracy, f1

def create_emb_layer(weights_matrix, non_trainable=False):
    num_embeddings, embedding_dim = weights_matrix.size()
    emb_layer = nn.Embedding(num_embeddings, embedding_dim)
    emb_layer.load_state_dict({'weight': weights_matrix})
    if non_trainable:
        emb_layer.weight.requires_grad = False

    return emb_layer  #, num_embeddings, embedding_dim

def weight_matrix(embedding_size, target_vocab, word_embeddings):
    '''creat a look-up table for word embeddings'''
    matrix_len = len(target_vocab)

    matrix = torch.zeros((matrix_len, embedding_size))
    #matrix[0] = torch.Tensor(np.random.normal(scale=0.6, size=(embedding_size, )))
    #vector of padding word

    for i in range(2, len(target_vocab)):
        try:
            matrix[i] = word_embeddings[target_vocab[i]]
        except KeyError:
            matrix[i] = torch.Tensor(
                np.random.normal(scale=0.6, size=(embedding_size, )))
    return matrix

def predict(vocab, tokens, label, max_sen_len, model):
    '''predict the class of a single text'''
    model = model.to(device)
    model.eval()
    padded = doc_padding(tokens, max_sen_len)
    x = torch.LongTensor([vocab.doc2idx(padded, 1)]).to(device)
    y_pred = model(x)
    predicted = torch.max(y_pred.cpu().data, 1)[1]
    print(' '.join(tokens))
    print('predict class:', predicted, "True class:", label)

def extract_and_save_biterm(fname, embed_size=100, min_count=10, max_percent=0.4):
    '''
    simple precocess of biterm

    A biterm is an unordered words pair
    Biterm is drawn from documents not from the whole corpus
    '''

    docs = read_corpus(fname, labeled=False, tokens_only=True)
    docs = [[token for token in doc if not token.isnumeric()] for doc in docs]

    # Remove words that are only one character.
    docs = [[token for token in doc if len(token) > 1] for doc in docs]

    lemmatizer = WordNetLemmatizer()
    docs = [[lemmatizer.lemmatize(token) for token in doc] for doc in docs]

    dictionary = Dictionary(docs)
    dictionary.filter_extremes(no_below=min_count, no_above=max_percent)
    dictionary.compactify()
    '''encode'''
    docs = [[token for token in doc if token in dictionary.token2id]
            for doc in docs]

    # # Remove docs that contains less than 3 words
    docs = [doc for doc in docs if len(set(doc)) > 1]
    # remove docs that contain less than 2 unique words
    model = gensim.models.Word2Vec(docs,
                                   workers=4,
                                   size=embed_size,
                                   iter=100,
                                   min_count=2)

    docs = [dictionary.doc2idx(doc) for doc in docs]

    biterms = {}
    i = 0
    doc_bitems = []
    for count, doc in enumerate(docs):
        d_bi = {}
        doc = sorted(doc)
        for x in range(len(doc) - 1):
            for y in range(x + 1, len(doc)):
                if doc[x] == doc[y]:
                    continue
                biterm = (doc[x], doc[y])
                idx = 0
                if biterm not in biterms:
                    biterms[biterm] = i
                    idx = i
                    i += 1
                else:
                    idx = biterms[biterm]
                if idx in d_bi:
                    d_bi[idx] += 1
                else:
                    d_bi[idx] = 1
        doc_bitems.append(d_bi)
    fname = os.path.basename(fname)
    fname = fname.split('.')[0]
    dirc = os.path.join(os.getcwd(), 'Data', 'unsupervised')
    if not os.path.exists(dirc):
        os.makedirs(dirc)

    embeddings = {}
    for key, token in dictionary.iteritems():
        embeddings[key] = model.wv[token]

    dictionary.save(os.path.join(dirc, fname + '_dic.pkl'))
    biterms = dict([key, val] for val, key in biterms.items())

    with open(os.path.join(dirc, fname + '_bit.pkl'), 'wb') as f:
        pickle.dump(biterms, f)
    with open(os.path.join(dirc, fname + '_doc_bit.pkl'), 'wb') as f:
        pickle.dump(doc_bitems, f)
    with open(os.path.join(dirc, fname + '_emb.pkl'), 'wb') as f:
        pickle.dump(embeddings, f)

class Dataset2(object):
    '''load dataset'''
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.model = None
        self.docs = None
        self.vocab = None
        self.word_embeddings = {}
        self.doc_embeddings = None

    def __getitem__(self, item):
        return self.corpus[item]

    def set_config(self, config):
        self.config = config

    def load_data(self, fname, padding):
        docs = read_corpus(fname, labeled=False)
        docs = [[token for token in doc if not token.isnumeric()]
                for doc in docs]

        # Remove words that are only one character.
        docs = [[token for token in doc if len(token) > 1] for doc in docs]

        lemmatizer = WordNetLemmatizer()
        docs = [[lemmatizer.lemmatize(token) for token in doc] for doc in docs]

        bigram = Phrases(docs, min_count=20)
        for idx in range(len(docs)):
            for token in bigram[docs[idx]]:
                if '_' in token:
                    # Token is a bigram, add to document.
                    docs[idx].append(token)

        # tagged = [
        #     gensim.models.doc2vec.TaggedDocument(tokens, [i])
        #     for i, tokens in enumerate(docs)
        # ]

        # model = models.doc2vec.Doc2Vec(tagged,
        #                                vector_size=self.config.embed_size,
        #                                min_count=0,
        #                                epochs=30,
        #                                workers=4,
        #                                dm_concat=1,
        #                                dm_tag_count=1)
        self.vocab = Dictionary(docs)
        if padding:
            docs = [doc_padding(x, self.config.max_sen_len) for x in docs]
            special_tokens = {'<pad>': 0, '<unk>': 1}
            self.vocab.patch_with_special_tokens(special_tokens)

        self.idxdoc = [self.vocab.doc2idx(x, 1) for x in docs]
        self.biterm = [self.vocab.doc2bow(doc) for doc in docs]
        # self.doc_embeddings = [model.docvecs[i] for i in range(len(docs))]

        # self.word_embeddings = torch.zeros(
        #     (len(self.vocab), self.config.embed_size))
        # for i in range(2, len(self.vocab)):
        #     word = self.vocab[i]
        #     try:
        #         self.word_embeddings[i] = torch.Tensor(
        #             model.wv.get_vector(word))
        #     except KeyError:
        #         self.word_embeddings[i] = torch.Tensor(
        #             np.random.normal(scale=0.6,
        #                              size=(self.config.embed_size, )))

class Dataset(object):
    '''load data'''
    def __init__(self, config):
        self.config = config
        self.train_data = None
        self.test_data = None
        self.val_data = None
        self.vocab = None
        self.word_embeddings = None

    def get_pandas_df(self, filename):
        '''
        Load the data into Pandas.DataFrame object
        This will be used to convert data to torchtext object
        '''
        with open(filename, 'r', encoding='utf-8') as datafile:
            data = [line.strip().split(' ', maxsplit=1) for line in datafile]
            data_text = list(map(lambda x: x[1], data))
            data_label = list(map(lambda x: x[0], data))

        full_df = pd.DataFrame({"text": data_text, "label": data_label})
        return full_df

    def load_data(self,
                  train_file,
                  test_file,
                  embed_file=None,
                  val_file=None,
                  voc_file='vocab.txt',
                  new_embed='word_embeddings.pkl'):
        '''
        Loads the data from files
        Sets up iterators for training, validation and test data
        Also create vocabulary and word embeddings based on the data
        
        Inputs:
            embed_file (String): absolute path to file containing word embeddings (GloVe/Word2Vec)
            train_file (String): absolute path to training file
            test_file (String): absolute path to test file
            val_file (String): absolute path to validation file
        '''
        #load embeddings

        train_X, train_Y = read_labeled(train_file)
        test_X, test_Y = read_labeled(test_file)
        val_X = None
        val_Y = None
        if val_file:
            val_X, val_Y = read_labeled(val_file)
        else:
            sp = int(len(train_X) * 0.8)
            train_X, val_X = (train_X[:sp], train_X[sp:])
            train_Y, val_Y = (train_Y[:sp], train_Y[sp:])
        train_X = [doc_padding(x, self.config.max_sen_len) for x in train_X]
        test_X = [doc_padding(x, self.config.max_sen_len) for x in test_X]
        val_X = [doc_padding(x, self.config.max_sen_len) for x in val_X]

        if os.path.isfile(voc_file):
            self.vocab = Dictionary.load_from_text(voc_file)
        else:
            self.vocab = Dictionary(train_X)
            special_tokens = {'<pad>': 0, '<unk>': 1}
            self.vocab.patch_with_special_tokens(special_tokens)
            self.vocab.save_as_text('vocab.txt')
        #build vocab
        train_X = [self.vocab.doc2idx(x, 1) for x in train_X]
        test_X = [self.vocab.doc2idx(x, 1) for x in test_X]
        val_X = [self.vocab.doc2idx(x, 1) for x in val_X]
        #transform words to index
        if os.path.isfile(new_embed):
            self.word_embeddings = torch.load(new_embed)
        else:
            embeds = Vectors(embed_file,
                             unk_init=lambda x: torch.Tensor(
                                 np.random.normal(scale=0.6, size=(x.size()))))
            self.word_embeddings = weight_matrix(self.config.embed_size,
                                                 self.vocab, embeds)
            torch.save(self.word_embeddings, "word_embeddings.pkl")
        self.train_data = (train_X, train_Y)
        self.test_data = (test_X, test_Y)
        self.val_data = (val_X, val_Y)

        print("Loaded {} training examples".format(len(train_X)))
        print("Loaded {} test examples".format(len(test_X)))
        print("Loaded {} validation examples".format(len(val_X)))

    def train_iterator(self):
        return batch_iter(*self.train_data, self.config.batch_size)

    def test_iterator(self):
        return batch_iter(*self.test_data, self.config.batch_size, False)

    def val_iterator(self):
        return batch_iter(*self.val_data, self.config.batch_size, False)

class TextCNN(nn.Module):
    def __init__(self, config, vocab_size, word_embeddings):
        super(TextCNN, self).__init__()
        self.config = config

        # Embedding Layer
        #self.embeddings = create_emb_layer(word_embeddings)
        self.embeddings = nn.Embedding(vocab_size, self.config.embed_size)
        self.embeddings.weight = nn.Parameter(word_embeddings,
                                              requires_grad=True)
        self.embeddings.padding_idx = 1

        self.conv1 = nn.Sequential(
            nn.Conv1d(in_channels=self.config.embed_size,
                      out_channels=self.config.num_channels,
                      kernel_size=self.config.kernel_size[0]), nn.ReLU(),
            nn.MaxPool1d(self.config.max_sen_len - self.config.kernel_size[0] +
                         1))
        self.conv2 = nn.Sequential(
            nn.Conv1d(in_channels=self.config.embed_size,
                      out_channels=self.config.num_channels,
                      kernel_size=self.config.kernel_size[1]), nn.ReLU(),
            nn.MaxPool1d(self.config.max_sen_len - self.config.kernel_size[1] +
                         1))
        self.conv3 = nn.Sequential(
            nn.Conv1d(in_channels=self.config.embed_size,
                      out_channels=self.config.num_channels,
                      kernel_size=self.config.kernel_size[2]), nn.ReLU(),
            nn.MaxPool1d(self.config.max_sen_len - self.config.kernel_size[2] +
                         1))

        self.dropout = nn.Dropout(self.config.dropout_keep)

        # Fully-Connected Layer
        self.fc = nn.Linear(
            self.config.num_channels * len(self.config.kernel_size),
            self.config.output_size)

        # Softmax non-linearity
        # self.softmax = nn.Softmax(dim=1)

    def forward(self, x):
        # x.shape = (max_sen_len, batch_size)
        embedded_sent = self.embeddings(x)
        embedded_sent = torch.transpose(embedded_sent, 1, 2)

        conv_out1 = self.conv1(embedded_sent).squeeze(2)
        conv_out2 = self.conv2(embedded_sent).squeeze(2)
        conv_out3 = self.conv3(embedded_sent).squeeze(2)

        all_out = torch.cat((conv_out1, conv_out2, conv_out3), 1)
        final_feature_map = self.dropout(all_out)
        final_out = self.fc(final_feature_map)
        # return self.softmax(final_out)
        return final_out

    def add_optimizer(self, optimizer):
        self.optimizer = optimizer

    def add_loss_op(self, loss_op):
        self.loss_op = loss_op

    def reduce_lr(self):
        print("Reducing LR")
        for g in self.optimizer.param_groups:
            g['lr'] = g['lr'] / 2

    def run_epoch(self, train_data, val_data, epoch):
        losses = []
        self.train()

        train_iterator = batch_iter(*train_data, self.config.batch_size)

        for i, batch in enumerate(train_iterator):
            self.optimizer.zero_grad()
            x = batch[0].to(device)
            y = batch[1].to(device)
            y_pred = self.__call__(x)
            loss = self.loss_op(y_pred, y)
            loss.backward()
            losses.append(loss.data.cpu().numpy())
            self.optimizer.step()

        val_iterator = batch_iter(*val_data, self.config.batch_size, False)
        print("Iter: {}".format(i + 1))
        avg_train_loss = np.mean(losses)
        print("\tAverage training loss: {:.5f}".format(avg_train_loss))

        # Evalute Accuracy on validation set
        val_accuracy, val_f1 = evaluate_su_model(self, val_iterator)
        print("\tVal Accuracy: {:.4f}, Val F1: {:.4f}".format(
            val_accuracy, val_f1))

        return avg_train_loss, val_accuracy, val_f1

class NBTM(nn.Module):
    def __init__(self,
                 num_topics,
                 vocab_size,
                 t_hidden_size,
                 embeddings,
                 theta_act="tanh",
                 train_embeddings=False,
                 enc_drop=0.5):
        super(NBTM, self).__init__()

        ## define hyperparameters
        self.num_topics = num_topics
        self.vocab_size = vocab_size
        self.t_hidden_size = t_hidden_size
        self.enc_drop = enc_drop
        self.t_drop = nn.Dropout(enc_drop)

        self.theta_act = self.get_activation(theta_act)

        ## define the word embedding matrix \rho
        num_embeddings, emb_size = embeddings.size()
        self.emb_size = emb_size
        with torch.no_grad():
            self.rho = embeddings.clone().float().to(device)
        ## creat word embeddings

        ## define the matrix containing the topic embeddings
        self.alphas = nn.Linear(
            emb_size, num_topics,
            bias=False)  #nn.Parameter(torch.randn(emb_size, num_topics))

        ## define variational distribution for \theta_{1:D} via amortizartion
        self.q_theta = nn.Sequential(
            nn.Linear(emb_size, t_hidden_size),
            self.theta_act,
            nn.Linear(t_hidden_size, t_hidden_size),
            self.theta_act,
        )
        self.mu_q_theta = nn.Linear(t_hidden_size, num_topics, bias=True)
        self.logsigma_q_theta = nn.Linear(t_hidden_size, num_topics, bias=True)

    def get_activation(self, act):
        if act == 'tanh':
            act = nn.Tanh()
        elif act == 'relu':
            act = nn.ReLU()
        elif act == 'softplus':
            act = nn.Softplus()
        elif act == 'rrelu':
            act = nn.RReLU()
        elif act == 'leakyrelu':
            act = nn.LeakyReLU()
        elif act == 'elu':
            act = nn.ELU()
        elif act == 'selu':
            act = nn.SELU()
        elif act == 'glu':
            act = nn.GLU()
        else:
            act = nn.Tanh()
        return act

    def gaussian(self, mu, logvar):
        """Returns a sample from a Gaussian distribution via reparameterization.
        """
        if self.training:
            std = torch.exp(0.5 * logvar)
            eps = torch.randn_like(std)
            return eps.mul_(std).add_(mu)
        else:
            return mu

    def encode(self, biterms):
        """Returns paramters of the variational distribution for \\theta.

        input: biterms vectors with size batch_size * emb_size
        output: mu_theta, log_sigma_theta
        """
        q_theta = self.q_theta(biterms)
        if self.enc_drop > 0:
            q_theta = self.t_drop(q_theta)
        mu_theta = self.mu_q_theta(q_theta)
        logsigma_theta = self.logsigma_q_theta(q_theta)
        kl_theta = -0.5 * torch.sum(
            1 + logsigma_theta - mu_theta.pow(2) - logsigma_theta.exp(),
            dim=-1).mean()
        return mu_theta, logsigma_theta, kl_theta

    def get_beta(self):
        '''return the probability of P(w|z)'''
        try:
            logit = self.alphas(
                self.rho.weight)  # torch.mm(self.rho, self.alphas)
        except:
            logit = self.alphas(self.rho)
        beta = F.softmax(logit,
                         dim=0).transpose(1,
                                          0)  ## softmax over vocab dimension
        return beta

    def get_theta(self, biterms):
        '''return topic distribution \\theat'''
        mu_theta, logsigma_theta, kld_theta = self.encode(biterms)
        ## mu, sigma of Gaussian, kld_theaa
        z = self.gaussian(mu_theta, logsigma_theta)
        theta = F.softmax(z, dim=-1)
        return theta, kld_theta

    def decode(self, theta, beta):
        res = torch.mm(theta, beta)
        preds = torch.log(res + 1e-6)
        return preds

    def forward(self, bi_idx, biterms, theta=None, aggregate=True):
        ## get \theta
        if theta is None:
            theta, kld_theta = self.get_theta(biterms)
        else:
            kld_theta = None

        ## get \\beta
        beta = self.get_beta()

        ## get prediction loss
        preds = self.decode(theta, beta)
        recon_loss = - torch.masked_select(preds, bi_idx)
        # recon_loss = -(bi_idx * preds).sum(1)
        if aggregate:
            recon_loss = recon_loss.mean()
        return recon_loss, kld_theta

    def run_epoch(self, biterms, epoch):
        losses = []
        self.train()
        train_iterator = batch_iter2(biterms, self.rho, vocab_size=self.vocab_size, batch_size=20480)
        for i, batch in enumerate(train_iterator):
            self.optimizer.zero_grad()
            self.zero_grad()
            idx = batch[0].to(device)
            biterms = batch[1].to(device)
            recon_loss, kld_theta = self.forward(idx, biterms)
            total_loss = recon_loss + kld_theta
            total_loss.backward()
            self.optimizer.step()
            print(total_loss)

    def add_optimizer(self, optimizer):
        self.optimizer = optimizer

    def infer(self, doc, biterm_dic):
        '''infer topic distribution of a document
        doc: a set of biterm (idx, count)
        '''
        biterms = list(doc.keys())
        with torch.no_grad():
            sum_biterms = sum(doc.values())
            p_b_ds = list(doc.values())
            p_b_ds = [x / sum_biterms for x in p_b_ds]
            p_b_ds = torch.Tensor(p_b_ds).to(device)
            #decode p_b_d = p(b|d)

            emb_batch = torch.zeros(len(biterms), self.emb_size).to(device)
            for i in range(len(biterms)):
                w1, w2 = biterm_dic[biterms[i]]
                emb_batch[i] = self.rho[w1] + self.rho[w2]

            theta, kld_theta = self.get_theta(emb_batch)
            beta = self.get_beta()

            p_z_bs = torch.zeros(self.num_topics, len(biterms)).to(device)
            for i in range(len(biterms)):
                w1, w2 = biterm_dic[biterms[i]]
                for j in range(self.num_topics):
                    p_z_bs[j][i] = theta[0][j] * beta[j][w1] * beta[j][w2]
            sum_zb = p_z_bs.sum(1)
            for i in range(len(sum_zb)):
                p_z_bs[i] /= sum_zb[i]
            #p_z_b = p(z|b)

            p_z_ds = torch.zeros(self.num_topics).to(device)
            for i in range(self.num_topics):
                for j in range(len(biterms)):
                    p_z_ds[i] += p_z_bs[i][j] * p_b_ds[j]
            #p_z_d = p(z|d)
            return p_z_ds.cpu().numpy()

    def show_topics(self, vocab, max_words):
        '''
        Display generated topics with \param max_words words
        '''
        with torch.no_grad():
            betas = self.get_beta()
            topics = []
            for k in range(self.num_topics):
                beta = betas[k]
                top_words = list(beta.cpu().numpy().argsort()[-max_words +
                                                            1:][::-1])
                topic_words = [vocab[a] for a in top_words]
                topics.append(' '.join(topic_words))
                print('Topic {}: {}'.format(k, topic_words))
            return topics