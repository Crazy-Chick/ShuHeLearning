import torch
import torch.nn as nn
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence
from embeddings import Embeddings


class NMT(nn.Module):

    def __init__(self, text, options, device):
        super(NMT, self).__init__()
        self.options = options
        self.embeddings = Embeddings(options.embed_size, text)
        self.hidden_size = options.hidden_size
        self.window_size_d = options.window_size_d
        self.text = text
        self.device = device
        self.encoder_layer = options.encoder_layer 
        self.decoder_layers = options.decoder_layers

        self.encoder = nn.LSTM(input_size=options.embed_size, hidden_size=options.hidden_size, num_layers=options.encoder_layer, bias=True, dropout=options.dropout_rate, bidirectional=False)
        self.decoder = nn.LSTM(input_size=options.embed_size+options.hidden_size, hidden_size=options.hidden_size, num_layers=options.decoder_layers, bias=True, dropout=options.dropout_rate, bidirectional=False)
        self.ht2tan = nn.Linear(in_features=self.hidden_size, out_features=self.hidden_size, bias=False)
        self.tan2pt = nn.Linear(in_features=self.hidden_size, out_features=1, bias=False)
        self.ct2ht = nn.Linear(in_features=self.hidden_size*2, out_features=self.hidden_size, bias=False)
        self.ht2final = nn.Linear(in_features=self.hidden_size, out_features=len(self.text.tar), bias=False)
    
    def forward(self, source, target):
        len_ = []
        for sen in source:
            len_.append(len(sen))
        source_tensor = self.text.src.word2tensor(source, self.device).cuda()
        target_tensor = self.text.tar.word2tensor(target, self.device).cuda()
        encode_h, encode_len, encode_hn_cn = self.encode(source_tensor, len_)
        decode_out = self.decode(encode_hn_cn, encode_h, encode_len, target_tensor)
        P = nn.functional.log_softmax(self.ht2final(decode_out), dim=-1)  # sen_len * batch * vocab_size
        tar_mask = (target_tensor != self.text.tar['<pad>']).float()
        tar_log_pro = torch.gather(P, index=target_tensor[1:].unsqueeze(-1), dim=-1).squeeze(-1) * tar_mask[1:]
        return tar_log_pro.sum(dim=0)

    def encode(self, source_tensor, source_length):
        x = self.embeddings.src(source_tensor)
        source_length = torch.tensor(source_length, dtype=torch.int64)
        x = pack_padded_sequence(x, source_length.cpu(), enforce_sorted=False)
        output, (hn, cn) = self.encoder(x)
        output, each_len = pad_packed_sequence(output)
        output = output.permute(1, 0, 2)
        return output, each_len, (hn, cn)

    def decode(self, h0_c0, encode_h, encode_len, target_tensor):
        y = self.embeddings.tar(target_tensor)
        ht_ct = h0_c0
        ht = torch.zeros(encode_h.shape[0], self.hidden_size, device=self.device).cuda()
        output = []
        for y_t in y:
            now_ht_ct, now_ht = self.step(encode_h, encode_len, torch.cat((y_t, ht), dim=1).view(1, y.shape[1], -1), ht_ct)
            output.append(now_ht)
            ht_ct = now_ht_ct
            ht = now_ht
        return torch.stack(output).to(self.device).cuda() # sen_len * batch * hidden_size

    def step(self, encode_h, encode_len, pre_yt, pre_ht_ct):
        '''
        yt, ht_ct = self.decoder(pre_yt, pre_ht_ct)
        yt = torch.squeeze(yt, dim=0)
        pt = nn.functional.sigmoid(self.tan2pt(nn.functional.tanh(self.ht2tan(yt))))
        batch_ct = None
        with torch.no_grad():
            for i, each_pt in enumerate(pt):
                each_pt = encode_len[i].item() * each_pt.item()
                left = max(0, int(each_pt) - self.window_size_d)
                right = min(encode_len[i].item(), int(each_pt) + self.window_size_d)
                align = None
                for j in range(left, right):
                    if (j == left):
                        align = encode_h[i][j].view(1, -1)
                    else:
                        align = torch.cat((align, encode_h[i][j].view(1, -1)), dim=0)
                align = nn.functional.softmax(torch.squeeze(torch.bmm(yt[i].view(1, 1, -1), align.t().unsqueeze(dim=0)), dim=0).squeeze(dim=0))
                ex_p = torch.zeros(right-left, dtype=torch.float16)
                for j in range(left, right):
                    ex_p[j-left] = math.exp(-(j-each_pt)*(j-each_pt)/(self.window_size_d*self.window_size_d/2))
                ex_p = ex_p.to(self.device).cuda()
                align = align.to(self.device).cuda()
                at = align * ex_p
                ct = torch.zeros(self.hidden_size, dtype=torch.float16)
                ct = ct.to(self.device).cuda()
                for j in range(left, right):
                    ct += at[j-left]*encode_h[i][j]
                if (i == 0):
                    batch_ct = torch.cat((ct.view(1, -1), yt[i].view(1, -1)), dim=1)
                else:
                    batch_ct = torch.cat((batch_ct, torch.cat((ct.view(1, -1), yt[i].view(1, -1)), dim=1)), dim=0)
        #batch_ct = torch.zeros(pt.shape[0], self.hidden_size * 2, device=self.device)
        ht = nn.functional.tanh(self.ct2ht(batch_ct))
        batch_ct = None
        return ht_ct, ht
        '''
        encode_len = encode_len.cuda()
        yt, ht_ct = self.decoder(pre_yt, pre_ht_ct)
        yt = torch.squeeze(yt, dim=0) # batch * hidden_size
        batch_size = yt.shape[0]
        pt = nn.functional.sigmoid(self.tan2pt(nn.functional.tanh(self.ht2tan(yt)))).reshape(yt.shape[0]) * encode_len # batch
        pt = pt.reshape(batch_size, 1)  # batch * 1
        #with torch.no_grad():
        # encode_h : batch * sen_len * hidden_size
        pre_align = torch.bmm(yt.reshape(batch_size, 1, self.hidden_size), torch.transpose(encode_h, 1, 2)).squeeze(dim=1) # batch * sen_len
        for i in range(batch_size):
            pre_align[i][encode_len[i].item():] = float('-inf')
        align = nn.functional.softmax(pre_align, dim=-1) # batch * sen_len
        per_s = torch.arange(0, encode_h.shape[1], dtype=torch.long).reshape(1, encode_h.shape[1])
        S = per_s
        for i in range(batch_size-1):
            S = torch.cat((S, per_s), dim=0)
        S = S.cuda()
        at = align * torch.exp(-(torch.pow(S-pt, 2)/(self.window_size_d*self.window_size_d/2))) # batch * sen_len
        at = at.reshape(batch_size, -1, 1)
        pre_ct = at * encode_h # batch * sen_len * hidden_size
        ct = torch.cat((pre_ct.sum(dim=1), yt), dim=-1)
        ht = nn.functional.tanh(self.ct2ht(ct))
        return ht_ct, ht
        
    
    def beam_search(self, src, search_size, max_tar_length):
        src_tensor = self.text.src.word2tensor(src, self.device)
        all_h, encode_len, (h_n, c_n) = self.encode(src_tensor, [len(src)])
        # all_h: 1 * sen_len * hidden_size
        sen_len = all_h.shape[1]
        new_all_h = all_h
        for i in range(4):
            new_all_h = torch.cat((new_all_h, all_h), dim=0)
        # new_all_h: 5 * sen_len * hidden_size
        all_h = new_all_h
        all_h = all_h.cuda()
        encode_len = []
        for i in range(5):
            encode_len.append(len(src))
        encode_len = torch.tensor(encode_len, dtype=torch.long, device=self.device)
        encode_len = encode_len.cuda()
        h_n = h_n.cuda()
        c_n = c_n.cuda()
        now_h = h_n
        now_c = c_n
        end_id = self.text.tar['<end>']
        now_predict = [[self.text.tar['<start>']]]
        now_predict_words = [self.text.tar['<start>']]
        now_batch_word_tensor = torch.cat((self.embeddings.tar(torch.tensor([self.text.tar['<start>']], dtype=torch.long, device=self.device).cuda()), torch.zeros(1, self.hidden_size, dtype=torch.float, device=self.device).cuda()), dim=-1).reshape(1, 1, -1)
        predict = []
        now_predict_length = 0
        now_score = torch.zeros(1, dtype=torch.float, device=self.device).reshape(1, 1)
        while (len(predict) < search_size and now_predict_length < max_tar_length):
            now_predict_length += 1
            next_ht_ct, next_ht = self.step(all_h[:len(now_predict)].reshape(len(now_predict), sen_len, -1), encode_len[:len(now_predict)], now_batch_word_tensor, (now_h, now_c))
            now_h, now_c = next_ht_ct
            now_h = now_h.permute(1, 0, 2)
            now_c = now_c.permute(1, 0, 2)
            P = (nn.functional.log_softmax(self.ht2final(next_ht), dim=-1)+now_score).reshape(next_ht.shape[0]*len(self.text.tar))
            score, topk_index = torch.topk(P, 5)
            next_predict_words = []
            next_predict = []
            next_score = []
            next_h = None
            next_c = None
            now_final_h = None
            for i in range(5):
                next_word_id = topk_index[i].item() % len(self.text.tar)
                batch_id = topk_index[i].item() // len(self.text.tar)
                if (next_word_id == end_id):
                    predict.append(now_predict[batch_id][1:].copy())
                    if (len(predict) == search_size):
                        break
                    continue
                next_predict_words.append(next_word_id)
                shuhe = now_predict[batch_id].copy()
                shuhe.append(next_word_id)
                next_predict.append(shuhe)
                if (next_h is None):
                    next_h = now_h[batch_id].reshape(1, 4, -1)
                    next_c = now_c[batch_id].reshape(1, 4, -1)
                    now_final_h = next_ht[batch_id].reshape(1, -1)
                    next_score.append(score[i].item())
                else:
                    next_h = torch.cat((next_h, now_h[batch_id].reshape(1, 4, -1)), dim=0)
                    next_c = torch.cat((next_c, now_c[batch_id].reshape(1, 4, -1)), dim=0)
                    now_final_h = torch.cat((now_final_h, next_ht[batch_id].reshape(1, -1)), dim=0)
                    next_score.append(score[i].item())
            if (len(predict) == search_size):
                break
            if (now_predict_length == max_tar_length):
                for sen in next_predict:
                    predict.append(sen[1:])
                    if (len(predict) == search_size):
                        break
            now_predict_words = next_predict_words
            now_predict = next_predict
            now_h = next_h.view(4, next_h.shape[0], -1).contiguous()
            now_c = next_c.view(4, next_c.shape[0], -1).contiguous()
            now_batch_word_tensor = torch.cat((self.embeddings.tar(torch.tensor(now_predict_words, dtype=torch.long, device=self.device)), now_final_h), dim=1)
            now_batch_word_tensor = now_batch_word_tensor.reshape(1, now_batch_word_tensor.shape[0], now_batch_word_tensor.shape[1])
            now_score = torch.tensor(next_score, dtype=torch.float, device=self.device).reshape(-1, 1)
        return predict

    @staticmethod
    def load(model_path):
        params = torch.load(model_path, map_location=lambda storage, loc: storage)
        model = NMT(params['text'], params['options'], params['device'])
        model.load_state_dict(params['state_dict'])
        return model
    
    def save(self, model_path):
        print(f"save model to path [{model_path}]")
        params = {
            'text': self.text,
            'options': self.options,
            'device': self.device,
            'state_dict': self.state_dict()
        }
        torch.save(params, model_path)