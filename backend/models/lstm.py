from abc import ABC, abstractmethod
from collections import Counter
from pathlib import Path
import re
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.utils.rnn import pad_sequence
import matplotlib.pyplot as plt
from numpy import asarray, log
import pandas as pd
from sklearn.model_selection import train_test_split, KFold
from sklearn.metrics import classification_report, precision_score, recall_score

def filter_text(text):
    """
    Filters text into text which only contain alphabet characters
    and removes all twitter handles
    """
    alphabet = re.compile("[^A-Za-z ]+")
    text = re.sub("@(\w){1,15}", "", text)
    text = re.sub("\n", " ", text)
    text = re.sub("'", "", text)
    text = alphabet.sub(" ", text).split(" ")
    words = filter(lambda x: x != "", text)
    words = list(map(lambda x: x.lower(), words))
    return words

def get_vocab(documents):
    """Gets the vocabular defined by the documents in the dataset"""
    vocab = set()
    for document in documents:
        for word in document:
            vocab.add(word)
    return vocab

def encode_documents(documents, vocab):
    """
    Encodes words in the document so that they can be
    be used in Glove embedding
    """
    for i, document in enumerate(documents):
        for j, word in enumerate(document):
            documents[i][j] = vocab[word]
    return documents

def get_weights(file_name, vocab, add_zero=False):
    """
    Gets weights from specified glove file
    """
    word_indices = {}
    weights = []
    with open(file_name, "r") as f:
        for i, line in enumerate(f):
            line = line.split(" ")
            word_indices[line[0]] = i
            weights.append(np.asarray(line[1:]).astype(np.float64))
    idx = len(weights)
    for word in vocab:
        if word not in word_indices:
            weights.append(np.random.random(weights[0].shape))
            word_indices[word] = idx
            idx += 1
    if add_zero:
        weights.append(np.zeros(weights[0].shape))
    return word_indices, np.asarray(weights)

class GloveEmbedding(nn.Embedding):
    """
    Creates an embedding layer which takes in pre-trained glove embeddings
    and uses those weights for the embedding layer
    """
    def __init__(self, weights, *args, train_embedding=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.load_state_dict({'weight': weights})
        # Setting these to false makes it so it does
        # not update glove embeddings when doing backprop
        self.requires_grad = train_embedding
        self.weight.requires_grad = train_embedding

def move_device(*args, available=False, **kwargs):
    new_args = []
    new_kwargs = {}
    for arg in args:
        if hasattr(arg, 'cuda'):
            if available:
                arg = arg.cuda()
        new_args.append(arg)
    for key, value in kwargs.items():
        if hasattr(value, 'cuda'):
            if available:
                value = value.cuda()
        new_kwargs[key] = value
    return new_args, new_kwargs

def move_back(arg_devices, kwarg_devices, *args, **kwargs):
    new_args = []
    new_kwargs = {}
    for i, arg in enumerate(args):
        if hasattr(arg, 'cuda'):
            arg = arg.to(arg_devices[i])
        new_args.append(arg)
    for key, value in kwargs.items():
        if hasattr(value, 'cuda'):
            value = value.to[kwarg_devices[key]]
        new_kwargs[key] = value
    return new_args, new_kwargs

def use_gpu(available=False):
    def wrapper(f):
        def new_fun(*args, **kwargs):
            arg_devices = [None for i in range(len(args))]
            kwarg_devices = {}
            for i, arg in enumerate(args):
                if hasattr(arg, 'cuda'):
                    arg_devices[i] = arg.device
            for key, value in kwargs.items():
                if hasattr(value, 'cuda'):
                    kwarg_devices[key] = value.device
            args, kwargs = move_device(*args, available=available, **kwargs)
            rv = f(*args, **kwargs)
            args, kwargs = move_back(arg_devices, kwarg_devices, *args, kwargs)
            return rv
        return new_fun
    return wrapper

class ModelBaseClass(nn.Module, ABC):

    def __init__(self, device=0, use_tensorboard=False):
        super().__init__()
        self.device = device
        self.use_tensorboard = use_tensorboard
        if use_tensorboard:
            writer = SummaryWriter()

    @property
    @abstractmethod
    def optimizer(self):
        pass

    @property
    @abstractmethod
    def loss(self):
        pass

    @abstractmethod
    def forward(self, inputs):
        pass

    def print_loss(self, iteration, epochs, batch_number, batch_size, loss):
        loading_chars = "\/—"
        loading_char = loading_chars[batch_number%len(loading_chars)]
        print(
            f'\rIteration {iteration}/{epochs} Batch {batch_number}/{batch_size}: Loss Value: {loss} {loading_char}',
            end=""
        )

    def get_indices(self, dataset_size, batch_size):
        shuffled_indices = np.random.permutation(dataset_size)
        ending_index = dataset_size - (dataset_size % batch_size)
        batch_indices = [shuffled_indices[range(i, i+batch_size)]
                         for i in range(0, ending_index, batch_size)]
        if ending_index != dataset_size:
            batch_indices.append(shuffled_indices[range(ending_index, dataset_size)])
        return batch_indices

    @torch.no_grad()
    def validation(self, data, targets):
        predictions = self.forward(data)
        loss = self.loss(predictions, targets)
        return loss.item()

    def step(self, input_data, expected_output):
        self.optimizer().zero_grad()
        predictions = self.forward(input_data)
        loss = self.loss()(predictions, expected_output)
        loss.backward()
        self.optimizer().step()
        return loss.item()

    @use_gpu(available=True)
    def update(self, data, targets, batch_size=50, epochs=100):
        self.train()
        total_loss = 0
        data, targets = self.create_batches(
            data,
            targets,
            batch_size
        )
        for iteration in range(epochs):
            total_loss = 0
            for i, (input_val, target) in enumerate(zip(data, targets)):
                loss = self.step(input_val, target)
                total_loss += loss
                self.print_loss(iteration, epochs, i, len(data)-1, loss)
            total_loss /= len(data)
            self.print_loss(iteration, epochs, i, len(data)-1, total_loss)
            print("")
        return total_loss

    def get_batches(self, data, batch_indices):
        return [data[indices] for indices in batch_indices]

    def create_batches(self, input_data, expected_output, batch_size):
        dataset_size = input_data.shape[0]
        batch_indices = self.get_indices(dataset_size, batch_size)
        batched_inputs = self.get_batches(input_data, batch_indices)
        batched_outputs = self.get_batches(expected_output, batch_indices)
        return batched_inputs, batched_outputs

    def load_model(self, path):
        if not isinstance(path, Path):
            path = Path(path)
        if path.exists():
            model_dict = torch.load(path)
            self.load_state_dict(model_dict)
            return self

    def save_model(self, path):
        if not isinstance(path, Path):
            path = Path(path)
        if not self._path.parent.exists():
            self._path.parent.mkdir()
        torch.save(self.state_dict(), path)


class LSTM(ModelBaseClass):

    def __init__(self, weights, classes, *args, use_avg=False, hidden_size=64, **kwargs):
        super().__init__(*args, **kwargs)
        self.embedding = GloveEmbedding(weights,
                                        weights.shape[0],
                                        weights.shape[1],
                                        padding_idx=len(weights)-1)
        # Parameters declared with size 0
        self.hidden_size = hidden_size
        self.gru = nn.GRU(weights.shape[1], hidden_size, batch_first=True)
        self.linear = nn.Linear(hidden_size, classes)
        self.loss_function = nn.CrossEntropyLoss()
        self.adam = optim.Adam(self.parameters(), lr=1e-3)
        self.weight_shape = weights.shape
        self.use_avg = True

    def loss(self):
        return self.loss_function

    def optimizer(self):
        return self.adam
    @use_gpu(available=True)
    def forward(self, data):
        # Convert input to tensor if not already a tensor
        embedding = self.embedding(data)
        hidden = self.init_hidden(embedding.size())
        output, hidden = self.gru(embedding, hidden)
        output = self.linear(output)
        if not self.training:
            output = F.softmax(output, dim=-1)
        return output[:, -1, :]

    def init_hidden(self, input_shape):
        """
        Initializes the hidden state for GRU based on the
        input shape (important since batch sizes are not the same across different runs)
        """
        return torch.zeros(1,input_shape[0],
                           self.hidden_size,
                           device=self.device)

    def save(self, path):
        torch.save(self.state_dict(), path)

    def load(self, path):
        self.load_state_dict(torch.load(path))
        self.eval()

    def __call__(self, data):
        predictions = self.forward(data)
        return predictions

print(torch.cuda.is_available())
print(torch.cuda.device_count())
for i in range(torch.cuda.device_count()):
  print(torch.cuda.get_device_name(i))
print(torch.cuda.get_device_name(torch.device('cuda')))
device = torch.device('cuda')

data = pd.read_csv("./data/data.csv")
labels = data[['label']].values
contents = data[['content']].values.reshape(-1)
contents = [filter_text(content) for content in contents]
vocab = get_vocab(contents)
vocab_map, weights = get_weights("./data/glove.6B.50d.txt", vocab, add_zero=True)
weights = torch.from_numpy(weights)
contents = encode_documents(contents, vocab_map)
contents = [torch.Tensor(content) for content in contents]
contents = pad_sequence(contents,
                        batch_first=True,
                        padding_value=len(weights)-1).long()

model = LSTM(weights, int(max(labels.reshape(-1))+1), device=torch.device("cuda"), hidden_size=256)
labels = torch.from_numpy(labels.reshape(-1)).long()
print(labels)
print(contents.size())
print(labels.size())
total_loss = model.update(contents, labels, epochs=100)
print("Final Loss:", total_loss)

model.save("./models/model.pt")

sentence = "my gf loves me"
data = [sentence.split(" ")]
data = encode_documents(data, vocab_map)
print(data)
data = torch.Tensor(data).long()
model.eval()
print(torch.argmax(model(data)))
