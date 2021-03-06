import torch
from torch import nn
import torch.nn.functional as F
import numpy as np
from IFETEL2019.models import modelutils


def inference_labels_full(l1_type_indices, child_type_vecs, scores, extra_label_thres=0.5):
    label_preds_main = inference_labels(l1_type_indices, child_type_vecs, scores)
    label_preds = list()
    for i in range(len(scores)):
        extra_idxs = np.argwhere(scores[i] > extra_label_thres).squeeze(axis=1)
        label_preds.append(list(set(label_preds_main[i] + list(extra_idxs))))
    return label_preds


def inference_labels(l1_type_indices, child_type_vecs, scores):
    l1_type_scores = scores[:, l1_type_indices]
    tmp_indices = np.argmax(l1_type_scores, axis=1)
    max_l1_indices = l1_type_indices[tmp_indices]
    l2_scores = child_type_vecs[max_l1_indices] * scores
    max_l2_indices = np.argmax(l2_scores, axis=1)
    # labels_pred = np.zeros(scores.shape[0], np.int32)
    labels_pred = list()
    for i, (l1_idx, l2_idx) in enumerate(zip(max_l1_indices, max_l2_indices)):
        # labels_pred[i] = l2_idx if l2_scores[i][l2_idx] > 1e-4 else l1_idx
        labels_pred.append([l2_idx] if l2_scores[i][l2_idx] > 1e-4 else [l1_idx])
    return labels_pred


class BaseResModel(nn.Module):
    def __init__(self, device, type_vocab, type_id_dict, embedding_layer: nn.Embedding,
                 context_lstm_hidden_dim, type_embed_dim, dropout=0.5, concat_lstm=False):
        super(BaseResModel, self).__init__()
        self.device = device
        self.context_lstm_hidden_dim = context_lstm_hidden_dim
        self.dropout = dropout
        self.dropout_layer = nn.Dropout(dropout)


        self.type_vocab, self.type_id_dict = type_vocab, type_id_dict
        self.l1_type_indices, self.l1_type_vec, self.child_type_vecs = modelutils.build_hierarchy_vecs(
            self.type_vocab, self.type_id_dict)
        self.n_types = len(self.type_vocab)#128
        self.type_embed_dim = type_embed_dim#500
        self.type_embeddings = torch.tensor(np.random.normal(
            scale=0.01, size=(type_embed_dim, self.n_types)).astype(np.float32),
                                            device=self.device, requires_grad=True)
        self.type_embeddings = nn.Parameter(self.type_embeddings)#500,128

        self.criterion = nn.CrossEntropyLoss()
        self.word_vec_dim = embedding_layer.embedding_dim #300
        self.embedding_layer = embedding_layer

        self.concat_lstm = concat_lstm
        self.context_lstm1 = nn.LSTM(input_size=self.word_vec_dim, hidden_size=self.context_lstm_hidden_dim, #300,250
                                     bidirectional=True)
        self.context_hidden1 = None

        self.context_lstm2 = nn.LSTM(input_size=self.context_lstm_hidden_dim * 2, #500,250
                                     hidden_size=self.context_lstm_hidden_dim, bidirectional=True)
        self.context_hidden2 = None

    def get_loss(self, true_type_vecs, t, margin=1.0, person_loss_vec=None):
        loss = self.criterion(t, true_type_vecs)
        return loss

    def inference(self, scores, is_torch_tensor=True):
        if is_torch_tensor:
            scores = scores.data.cpu().numpy()
        return inference_labels(self.l1_type_indices, self.child_type_vecs, scores)

    def inference_full(self, logits, extra_label_thres=0.5, is_torch_tensor=True):
        if is_torch_tensor:
            logits = logits.data.cpu().numpy()
        return inference_labels_full(self.l1_type_indices, self.child_type_vecs, logits, extra_label_thres)

    def forward(self, *input_args):
        raise NotImplementedError
    def init_context_hidden(self, batch_size):
        return modelutils.init_lstm_hidden(self.device, batch_size, self.context_lstm_hidden_dim, True)

    def get_context_lstm_output(self, word_id_seqs, lens, mention_tok_idxs, batch_size):
        self.context_hidden1 = self.init_context_hidden(batch_size)
        self.context_hidden2 = self.init_context_hidden(batch_size)

        x = self.embedding_layer(word_id_seqs)#16,140,300
        # x = F.dropout(x, self.dropout, training)
        x = torch.nn.utils.rnn.pack_padded_sequence(x, lens, batch_first=True)
        lstm_output1, self.context_hidden1 = self.context_lstm1(x, self.context_hidden1)
        # lstm_output1 = self.dropout_layer(lstm_output1)
        lstm_output2, self.context_hidden2 = self.context_lstm2(lstm_output1, self.context_hidden2)

        lstm_output1, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_output1, batch_first=True)#16,140,500
        lstm_output2, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_output2, batch_first=True)
        if self.concat_lstm:
            lstm_output = torch.cat((lstm_output1, lstm_output2), dim=2)
        else:
            lstm_output = lstm_output1 + lstm_output2  #16,140,500

        lstm_output_r = lstm_output[list(range(batch_size)), mention_tok_idxs, :]
        # lstm_output_r = F.dropout(lstm_output_r, self.dropout, training)
        return lstm_output_r


class FETELStack(BaseResModel):
    def __init__(self, device, type_vocab, type_id_dict, embedding_layer: nn.Embedding, context_lstm_hidden_dim,
                 type_embed_dim, dropout=0.5, use_mlp=False, mlp_hidden_dim=None, concat_lstm=False):
        super(FETELStack, self).__init__(device, type_vocab, type_id_dict, embedding_layer,
                                         context_lstm_hidden_dim, type_embed_dim, dropout, concat_lstm)
        self.use_mlp = use_mlp
        self.alpha_scalar = nn.Parameter(torch.FloatTensor([.1]))
        # self.dropout_layer = nn.Dropout(dropout)
        # 929=500+300+128+1
        # linear_map_input_dim = 2 * self.context_lstm_hidden_dim + self.word_vec_dim + self.n_types + 1 #929  //464
        linear_map_input_dim = 2 * self.context_lstm_hidden_dim + self.word_vec_dim #800

        self.sigmoid= nn.Sigmoid()
        self.softmax=nn.Softmax(dim=1)
        self.leakyReLU=nn.LeakyReLU(0.1)
        if concat_lstm:
            linear_map_input_dim += 2 * self.context_lstm_hidden_dim
        if not self.use_mlp:
            self.linear_map = nn.Linear(linear_map_input_dim, type_embed_dim, bias=False)
        else:
            mlp_hidden_dim = linear_map_input_dim // 2 if mlp_hidden_dim is None else mlp_hidden_dim
            self.linear_map1 = nn.Linear(300, 200,True) #929,500
            self.linear_map2 = nn.Linear(200, self.n_types,True)
            # self.linear_map3 = nn.Linear(mlp_hidden_dim, type_embed_dim)#(500,500)
            self.linear_map3 = nn.Linear(linear_map_input_dim, mlp_hidden_dim,True)#(500,500)
            self.linear_map4 = nn.Linear(mlp_hidden_dim, self.n_types,True)#(500,500)

        # self.elmo_dim = self.elmo.get_output_dim()
        # self.attn_dim = 1
        # self.attn_inner_dim = self.elmo_dim
        # # Mention attention
        # self.men_attn_linear_m = nn.Linear(self.elmo_dim, self.attn_inner_dim, bias=False)
        # self.men_attn_linear_o = nn.Linear(self.attn_inner_dim, self.attn_dim, bias=False)
        # # Context attention
        # self.ctx_attn_linear_c = nn.Linear(self.elmo_dim, self.attn_inner_dim, bias=False)
        # self.ctx_attn_linear_m = nn.Linear(self.elmo_dim, self.attn_inner_dim, bias=False)
        # self.ctx_attn_linear_d = nn.Linear(1, self.attn_inner_dim, bias=False)
        # self.ctx_attn_linear_o = nn.Linear(self.attn_inner_dim,
        #                                    self.attn_dim, bias=False)
    def cross_entropy(self, predicted, truth):
        return -torch.sum(truth * torch.log(predicted + 1e-10)) \
               - torch.sum((1 - truth) * torch.log(1 - predicted + 1e-10))
    def forward(self, context_token_seqs, mention_token_idxs, mstr_token_seqs, entity_vecs, el_probs):
        batch_size = len(context_token_seqs)#16,140

        context_token_seqs, seq_lens, mention_token_idxs, back_idxs = modelutils.get_len_sorted_context_seqs_input(
            self.device, context_token_seqs, mention_token_idxs)
        context_lstm_output = self.get_context_lstm_output(
            context_token_seqs, seq_lens, mention_token_idxs, batch_size)#16,500
        context_lstm_output = context_lstm_output[back_idxs]


        name_output = modelutils.get_avg_token_vecs(self.device, self.embedding_layer, mstr_token_seqs)#16,300

        cat_output = self.dropout_layer(torch.cat((context_lstm_output, name_output), dim=1)) #16,800  参数的顺序好像是反的，应该mention在前面

        # l1_output = torch.tanh(self.linear_map1(name_output))#应该用name_output
        l1_output = self.linear_map1(name_output).tanh()
        vq = torch.relu(self.linear_map2(l1_output)) #16,128   dropout

        min_entity_vecs=torch.full_like(entity_vecs,-10)
        entity_vecs=torch.where(entity_vecs<1,min_entity_vecs,entity_vecs)
        cq=vq+entity_vecs
        pc=cq.softmax(1)

        l1_output = torch.tanh(self.linear_map3(cat_output))
        gq = torch.relu(self.linear_map4(l1_output))

        pg=gq.softmax(1) #应该用sigmoid,交叉熵需要
        # pg=self.softmax(gq) #应该用sigmoid,交叉熵需要
        p=self.alpha_scalar*pc+(1-self.alpha_scalar)*pg
        return p
