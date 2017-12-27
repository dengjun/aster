import logging
import functools

import tensorflow as tf
from tensorflow.contrib import seq2seq
from rare.core import predictor
from rare.core import loss


class BahdanauAttentionPredictor(predictor.Predictor):
  """Attention decoder based on tf.contrib.seq2seq"""

  def __init__(self,
               rnn_cell=None,
               rnn_regularizer=None,
               num_attention_units=None,
               max_num_steps=None,
               multi_attention=False,
               beam_width=None,
               reverse=False,
               label_map=None,
               loss=None,
               is_training=True):
    self._rnn_cell = rnn_cell
    self._rnn_regularizer = rnn_regularizer
    self._num_attention_units = num_attention_units
    self._max_num_steps = max_num_steps
    self._multi_attention = multi_attention
    self._beam_width = beam_width
    self._reverse = reverse
    self._label_map = label_map
    self._loss = loss
    self._is_training = is_training

    if not self._is_training and not self._beam_width > 0:
      raise ValueError('Beam width must be > 0 during inference')

  @property
  def start_label(self):
    return 0

  @property
  def end_label(self):
    return 1

  @property
  def num_classes(self):
    return self._label_map.num_classes + 2

  def predict(self, feature_maps, scope=None):
    if not isinstance(feature_maps, (list, tuple)):
      raise ValueError('`feature_maps` must be list of tuple')

    with tf.variable_scope(scope, 'Predict', feature_maps):
      def _build_attention_mechanism(memory):
        if not self._is_training:
          memory = tf.tile_batch(memory, multiplier=self._beam_width)
        return seq2seq.BahdanauAttention(
          self._num_attention_units,
          memory,
          memory_sequence_length=None
        )

      # build (possibly multiple) attention mechanisms
      feature_sequences = [tf.squeeze(map, axis=1) for map in feature_maps]
      if self._multi_attention:
        attention_mechanism = []
        for i, feature_sequence in enumerate(feature_sequences):
          memory = feature_sequence
          attention_mechanism.append(_build_attention_mechanism(memory))
      else:
        memory = tf.concat(feature_sequences, axis=1)
        attention_mechanism = _build_attention_mechanism(memory)

      attention_cell = seq2seq.AttentionWrapper(
        self._rnn_cell,
        attention_mechanism,
        output_attention=False)
      batch_size = shape_utils.combined_static_and_dynamic_shape(feature_maps[0])[0]
      embedding_fn = functools.partial(tf.one_hot, depth=num_classes)
      output_layer = tf.layers.Dense(
        num_classes,
        activation=None,
        use_bias=True,
        kernel_initializer=tf.variance_scaling_initializer(),
        bias_initializer=tf.zeros_initializer())
      if self._is_training:
        train_helper = seq2seq.TrainingHelper(
          embedding_fn(decoder_inputs),
          sequence_length=decoder_inputs_lengths,
          time_major=False)
        attention_decoder = seq2seq.BasicDecoder(
          cell=attention_cell,
          helper=train_helper,
          initial_state=attention_cell.zero_state(batch_size, tf.float32),
          output_layer=output_layer)
      else:
        batch_size = batch_size * self._beam_width
        attention_decoder = seq2seq.BeamSearchDecoder(
          cell=attention_cell,
          embedding=embedding_fn,
          start_tokens=tf.tile([start_label], [batch_size]),
          end_token=end_label,
          initial_state=attention_cell.zero_state(batch_size, tf.float32),
          beam_width=self._beam_width,
          output_layer=output_layer,
          length_penalty_weight=0.0)

      outputs, _, output_lengths = seq2seq.dynamic_decode(
        decoder=attention_decoder,
        output_time_major=False,
        impute_finished=False,
        maximum_iterations=self._max_num_steps)
      # apply regularizer
      filter_weights = lambda vars : [x for x in vars if x.op.name.endswith('kernel')]
      tf.contrib.layers.apply_regularization(
        self._rnn_regularizer,
        filter_weights(attention_cell.trainable_weights))

      outputs_dict = None
      if self._is_training:
        assert isinstance(outputs, seq2seq.BasicDecoderOutput)
        outputs_dict = {
          'labels': outputs.sample_id,
          'logits': outputs.rnn_output
        }
      else:
        assert isinstance(outputs, seq2seq.BeamSearchDecoderOutput)
        outputs_dict = {
          'labels': outputs.predicted_ids,
          'scores': outputs.scores
        }

    return outputs_dict, output_lengths

  def loss(self, predictions_dict, scope=None):
    assert 'logits' in predictions_dict
    with tf.variable_scope(scope, 'Loss', list(predictions_dict.values())):
      loss_tensor = self._loss(
        predictions_dict['logits'],
        self._groundtruth_dict['decoder_targets'],
        self._groundtruth_dict['decoder_lengths']
      )
    return loss_tensor

  def provide_groundtruth(self, groundtruth_text, scope=None):
    with tf.name_scope(scope, 'ProvideGroundtruth', [groundtruth_text]):
      batch_size = shape_utils.combined_static_and_dynamic_shape(groundtruth_text)[0]
      if self._reverse:
        groundtruth_text = ops.reverse_strings(groundtruth_text)
      text_labels, text_lengths = self._label_map.text_to_labels(
        groundtruth_text,
        pad_value=self.end_label,
        return_lengths=True)
      start_labels = tf.fill([batch_size, 1], tf.constant(self.start_label, tf.int64))
      end_labels = tf.fill([batch_size, 1], tf.constant(self.end_label, tf.int64))
      decoder_inputs = tf.concat([start_labels, start_labels, text_labels], axis=1)
      decoder_targets = tf.concat([start_labels, text_labels, end_labels])
      decoder_lengths = text_lengths + 2
      self._groundtruth_dict['decoder_inputs'] = decoder_inputs
      self._groundtruth_dict['decoder_targets'] = decoder_targets
      self._groundtruth_dict['decoder_lengths'] = decoder_lengths

  def postprocess(self, predictions_dict, scope=None):
    assert 'scores' in predictions_dict
    with tf.variable_scope(scope, 'Postprocess', list(predictions_dict.values())):
      text = self._label_map.labels_to_text(predictions_dict['labels'])
      if self._reverse:
        text = ops.string_reverse(text)
      scores = predictions_dict['scores']
    return {'text': text, 'scores': scores}