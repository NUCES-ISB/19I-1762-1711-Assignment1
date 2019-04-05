import re
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import Counter
from copy import deepcopy
from sklearn.base import BaseEstimator, ClassifierMixin
from torch.autograd import Variable
from torchtext.data import Dataset, Example, Field, Iterator, Pipeline


class CNNClassifier(BaseEstimator, ClassifierMixin):
    def __init__(self, lr=0.001, epochs=256, batch_size=64, test_interval=100,
                 early_stop=1000, save_best=True, dropout=0.5, max_norm=0.0,
                 embed_dim=128, kernel_num=100, kernel_sizes="3,4,5",
                 static=False, device=-1, cuda=True, class_weight=None,
                 split_ratio=0.9, random_state=None, vectors=None,
                 preprocessor=None):
        self.lr = lr
        self.epochs = epochs
        self.batch_size = batch_size
        self.test_interval = test_interval
        self.early_stop = early_stop
        self.save_best = save_best
        self.dropout = dropout
        self.max_norm = max_norm
        self.embed_dim = embed_dim
        self.kernel_num = kernel_num
        self.kernel_sizes = kernel_sizes
        self.static = static
        self.device = device
        self.cuda = cuda
        self.class_weight = class_weight
        self.split_ratio = split_ratio
        self.random_state = random_state
        self.vectors = vectors
        self.preprocessor = preprocessor

    def __clean_str(self, string):
        string = re.sub(r"[^A-Za-z0-9(),!?\'\`]", " ", string)
        string = re.sub(r"\'s", " \'s", string)
        string = re.sub(r"\'ve", " \'ve", string)
        string = re.sub(r"n\'t", " n\'t", string)
        string = re.sub(r"\'re", " \'re", string)
        string = re.sub(r"\'d", " \'d", string)
        string = re.sub(r"\'ll", " \'ll", string)
        string = re.sub(r",", " , ", string)
        string = re.sub(r"!", " ! ", string)
        string = re.sub(r"\(", " ( ", string)
        string = re.sub(r"\)", " ) ", string)
        string = re.sub(r"\?", " ? ", string)
        string = re.sub(r"\s{2,}", " ", string)
        return string.strip()

    def __eval(self, data_iter):
        self.__model.eval()

        corrects = 0

        for batch in data_iter:
            feature, target = batch.text, batch.label

            feature.data.t_()
            target.data.sub_(1)

            if self.cuda and torch.cuda.is_available():
                feature, target = feature.cuda(), target.cuda()

            logit = self.__model(feature)

            F.cross_entropy(logit, target, reduction="sum")

            predictions = torch.max(logit, 1)[1].view(target.size())
            corrects += (predictions.data == target.data).sum()

        return 100.0 * corrects / len(data_iter.dataset)

    def fit(self, X, y, sample_weight=None):
        train_iter, dev_iter = self.__preprocess(X, y, sample_weight)
        embed_num = len(self.__text_field.vocab)
        class_num = len(self.__label_field.vocab) - 1
        kernel_sizes = [int(k) for k in self.kernel_sizes.split(",")]
        self.__model = CNNText(embed_num, self.embed_dim, class_num,
                               self.kernel_num, kernel_sizes, self.dropout,
                               self.static)

        if self.cuda and torch.cuda.is_available():
            torch.cuda.set_device(self.device)
            self.__model.cuda()

        optimizer = torch.optim.Adam(self.__model.parameters(), lr=self.lr,
                                     weight_decay=self.max_norm)
        steps, best_acc, last_step = 0, 0, 0

        self.__model.train()

        for epoch in range(self.epochs):
            for batch in train_iter:
                feature, target = batch.text, batch.label

                feature.data.t_()
                target.data.sub_(1)

                if self.cuda and torch.cuda.is_available():
                    feature, target = feature.cuda(), target.cuda()

                optimizer.zero_grad()
                F.cross_entropy(self.__model(feature), target).backward()
                optimizer.step()

                steps += 1

                if steps % self.test_interval == 0:
                    dev_acc = self.__eval(dev_iter)

                    if dev_acc > best_acc:
                        best_acc = dev_acc
                        last_step = steps

                        if self.save_best:
                            best_model = deepcopy(self.__model)
                    elif steps - last_step >= self.early_stop:
                        if self.save_best:
                            self.__model = best_model

                        return self

        self.__model = best_model if self.save_best else self.__model
        return self

    def predict(self, X):
        y_pred = []
        max_krnl_sz = int(self.kernel_sizes[self.kernel_sizes.rfind(",") + 1:])

        for text in X:
            assert isinstance(text, str)

            text = self.__text_field.preprocess(text)

            if len(text) < max_krnl_sz:
                most_common = self.__label_field.vocab.freqs.most_common(1)[0]

                y_pred.append(most_common[0])
                continue

            self.__model.eval()

            text = [[self.__text_field.vocab.stoi[x] for x in text]]
            x = Variable(torch.tensor(text))
            x = x.cuda() if self.cuda and torch.cuda.is_available() else x
            _, predicted = torch.max(self.__model(x), 1)

            y_pred.append(self.__label_field.vocab.itos[predicted.data[0] + 1])

        return y_pred

    def __preprocess(self, X, y, sample_weight):
        self.__text_field = Field(lower=True)
        self.__label_field = Field(sequential=False)
        self.__text_field.preprocessing = Pipeline(self.__preprocess_text)
        fields = [("text", self.__text_field), ("label", self.__label_field)]
        weights = [1 for yi in y] if sample_weight is None else sample_weight
        exmpl = [Example.fromlist([X[i], y[i]], fields) for i in range(len(X))]

        if self.class_weight is not None:
            cw = self.class_weight

            if isinstance(cw, str) and cw == "balanced":
                counter = Counter(y)
                cw = [len(y) / (len(counter) * counter[yi]) for yi in y]
                weights = [weights[i] * cw[i] for i in range(len(y))]
            elif isinstance(cw, dict):
                cw = [cw[yi] for yi in y]
                weights = [weights[i] * cw[i] for i in range(len(y))]

        min_weight = min(weights)
        weights = [round(w / min_weight) for w in weights]

        for i in range(len(X)):
            if weights[i] > 1:
                Xi = [X[i] for j in range(weights[i] - 1)]
                exmpl += [Example.fromlist([x, y[i]], fields) for x in Xi]

        train_data, dev_data = Dataset(exmpl, fields).split(self.split_ratio,
                                                            self.random_state,)

        self.__text_field.build_vocab(train_data, dev_data,
                                      vectors=self.vectors)
        self.__label_field.build_vocab(train_data, dev_data)

        batch_sizes = (self.batch_size, len(dev_data))
        return Iterator.splits((train_data, dev_data), batch_sizes=batch_sizes,
                               sort_key=lambda ex: len(ex.text), repeat=False)

    def __preprocess_text(self, text):
        if self.preprocessor is None:
            return self.__clean_str(text)

        return self.preprocessor(text)


class CNNText(nn.Module):
    def __init__(self, embed_num, embed_dim, class_num, kernel_num,
                 kernel_sizes, dropout, static):
        super(CNNText, self).__init__()

        self.__embed = nn.Embedding(embed_num, embed_dim)
        Ks = kernel_sizes
        module_list = [nn.Conv2d(1, kernel_num, (K, embed_dim)) for K in Ks]
        self.__convs1 = nn.ModuleList(module_list)
        self.__dropout = nn.Dropout(dropout)
        self.__fc1 = nn.Linear(len(Ks) * kernel_num, class_num)
        self.__static = static

    def conv_and_pool(self, x, conv):
        x = F.relu(conv(x)).squeeze(3)
        return F.max_pool1d(x, x.size(2)).squeeze(2)

    def forward(self, x):
        x = self.__embed(x)

        if self.__static:
            x = Variable(x)

        x = x.unsqueeze(1)
        x = [F.relu(conv(x)).squeeze(3) for conv in self.__convs1]
        x = [F.max_pool1d(i, i.size(2)).squeeze(2) for i in x]
        return self.__fc1(self.__dropout(torch.cat(x, 1)))
