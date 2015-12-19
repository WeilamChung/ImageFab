#!/usr/bin/env python
import sys, os
from glob import glob
from random import choice
from io import BytesIO

from PIL import Image
import numpy as np
import tensorflow as tf

LEARNING_RATE = 0.1
TRAINING_ITERATIONS = 100000
TRAINING_DROPOUT_RATE = 0.8
TRAINING_REPORT_INTERVAL = 100
REPRESENTATION_SIZE = 64
BATCH_SIZE = 1
IMAGE_WIDTH = 256
IMAGE_HEIGHT = 256
IMAGE_DEPTH = 1

# Create model
def build_encoder(stream_to_encode, representation_size):
	"""Given the two streams, returns an encoder output and a decoder output."""
	w0 = tf.Variable(tf.random_normal([11, 11, IMAGE_DEPTH, 128]))
	b0 = tf.Variable(tf.random_normal([128,]))
	conv0 = tf.nn.conv2d(stream_to_encode, filter=w0, strides=[1, 1, 1, 1], padding='SAME') + b0
	act0 = tf.nn.relu(conv0)
	pool0 = tf.nn.max_pool(act0, ksize=[1, 11, 11, 1], strides=[1, 5, 5, 1], padding='SAME') # Squash depth, 1x1x128 -> 1x1x1

	w1 = tf.Variable(tf.random_normal([5, 5, 128, 64]))
	b1 = tf.Variable(tf.random_normal([64,]))
	conv1 = tf.nn.conv2d(pool0, filter=w1, strides=[1, 1, 1, 1], padding='SAME') + b1
	act1 = tf.nn.relu(conv1)
	pool1 = tf.nn.max_pool(act1, ksize=[1, 5, 5, 1], strides=[1, 5, 5, 1], padding='SAME') # Squash horizontally, leaving 1x1x64 per 5x5x128 chunk.

	flat = tf.reshape(pool1, [BATCH_SIZE, -1])
	
	w3 = tf.Variable(tf.random_normal([flat.get_shape().as_list()[-1], 512]))
	b3 = tf.Variable(tf.random_normal([512,]))
	mmul3 = tf.matmul(flat, w3) + b3
	act3 = tf.nn.relu(mmul3)

	w4 = tf.Variable(tf.random_normal([512, representation_size]))
	b4 = tf.Variable(tf.random_normal([representation_size,]))
	mmul4 = tf.matmul(act3, w4) + b4
	act4 = tf.nn.relu(mmul4)

	encoder = tf.identity(act4, name='encoder_output')

	return encoder, [w0, w1, w3, w4], [b0, b1, b3, b4]

def build_decoder(stream_to_decode, output_height, output_width, output_depth, weights=None, biases=None):
	if weights == None:
		w5 = tf.Variable(tf.random_normal([stream_to_decode.get_shape().as_list()[-1], 1024]))
		b5 = tf.Variable(tf.random_normal([1024,]))
	else:
		w5 = weights[0]
		b5 = biases[0]
	mmul5 = tf.matmul(stream_to_decode, w5) + b5
	act5 = tf.nn.relu(mmul5)

	if weights == None:
		w6 = tf.Variable(tf.random_normal([1024, 4096]))
		b6 = tf.Variable(tf.random_normal([4096,]))
	else:
		w6 = weights[1]
		b6 = biases[1]
	mmul6 = tf.matmul(act5, w6) + b6
	act6 = tf.nn.relu(mmul6)

	unflat = tf.reshape(act6, [-1, 32, 32, 4]) # b6 must be divisible by the product of whd.

	if weights == None:
		w7 = tf.Variable(tf.random_normal([5, 5, 16, 4]))
		b7 = tf.Variable(tf.random_normal([128, 128, 16]))
	else:
		w7 = weights[2]
		b7 = biases[2]
	deconv8 = tf.nn.deconv2d(unflat, filter=w7, strides=[1, 1, 1, 1], padding='SAME', output_shape=[1, 128, 128, 16]) + b7
	act8 = tf.nn.relu(deconv8)

	if weights == None:
		w8 = tf.Variable(tf.random_normal([11, 11, IMAGE_DEPTH, 16]))
		b8 = tf.Variable(tf.random_normal([output_height, output_width, output_depth]))
	else:
		w8 = weights[3]
		b8 = biases[3]
	deconv9 = tf.nn.deconv2d(act8, filter=w8, strides=[1, 1, 1, 1], padding='SAME', output_shape=[1, output_height, output_width, output_depth]) + b8

	return deconv9, [w5, w6, w7, w8], [b5, b6, b7, b8]

# Define objects
input_batch = tf.placeholder(tf.float32, [BATCH_SIZE, IMAGE_HEIGHT, IMAGE_WIDTH, IMAGE_DEPTH])
encoded_batch = tf.placeholder(tf.float32, [BATCH_SIZE, REPRESENTATION_SIZE]) # Replace BATCH_SIZE with None
keep_prob = tf.placeholder(tf.float32)

# Define data-source iterator
def gather_batch(file_glob, batch_size):
	reader = tf.WholeFileReader()
	filenames = glob(file_glob)
	while True:
		batch = np.zeros([batch_size, IMAGE_HEIGHT, IMAGE_WIDTH, IMAGE_DEPTH], dtype=np.float)
		num_samples = 0
		while num_samples < batch_size:
			try:
				filename = choice(filenames)
				img = Image.open(filename)
				target_width = IMAGE_WIDTH
				target_height = IMAGE_HEIGHT

				if IMAGE_DEPTH == 1:
					img = img.convert('L')
				elif IMAGE_DEPTH == 3:
					img = img.convert('RGB')
				else:
					raise Exception("Invalid depth argument for batch: {}".format(IMAGE_DEPTH))

				pad_min = True # Shrink down the image, then pad the smaller dimension with black.
				w = float(img.size[0])
				h = float(img.size[1])
				newimg = None
				if pad_min: # Pad the outside of the image.
					# Calculate new size
					max_res = max(w, h)
					new_width = int(target_width*float(w/max_res))
					new_height = int(target_height*float(h/max_res))
					# Center image in new image.
					newimg = Image.new(img.mode, (target_width, target_height))
					offset_x = (target_width//2)-(new_width//2)
					offset_y = (target_height//2)-(new_height//2)
					box = (offset_x, offset_y, offset_x+new_width, offset_y+new_height)
					newimg.paste(img.resize((new_width, new_height)), box)
				else: # Cut a section from the middle of the image.
					# Calculate size
					res_cap = min(w, h)
					new_width = int(target_width*(w/float(res_cap)))
					new_height = int(target_height*(h/float(res_cap)))
					# Cut image chunk.
					offset_x = (new_width//2)-(target_width//2)
					offset_y = (new_height//2)-(target_height//2)
					newimg = img.resize(
						(new_width, new_height)
					).crop(
						(offset_x, offset_y, offset_x+target_width, offset_y+target_height)
					)

				if newimg.size[0] != IMAGE_WDITH or newimg.size[1] != IMAGE_HEIGHT:
					print("Image smaller than target.  Skipping.")
					continue

				print("Loaded image {}".format(filename))
				# Another shim.  Depth == 3 has to be handled like this:
				if IMAGE_DEPTH == 3:
					batch[num_samples,:,:,:] = np.asarray(newimg, dtype=np.float)/255.0
				else:
					batch[num_samples,:,:,0] = np.asarray(newimg, dtype=np.float)/255.0
				num_samples += 1
			except ValueError as e:
				print("Problem loading image {}: {}".format(filename, e))
				continue
		yield batch
			
# Run!
with tf.Session() as sess:
	# Spin up data iterator.
	generator = gather_batch(sys.argv[1], BATCH_SIZE)

	# Get final ops
	encoder, _, _ = build_encoder(input_batch, REPRESENTATION_SIZE)
	decoder, dw, db = build_decoder(encoded_batch, IMAGE_HEIGHT, IMAGE_WIDTH, IMAGE_DEPTH)
	autoencoder, _, _ = build_decoder(encoder, IMAGE_HEIGHT, IMAGE_WIDTH, IMAGE_DEPTH, weights=dw, biases=db)
	l2_cost = tf.reduce_sum(tf.abs(input_batch - autoencoder))
	optimizer = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE).minimize(l2_cost)

	# Init variables.
	saver = tf.train.Saver()
	sess.run(tf.initialize_all_variables())

	# If we already have a trained network, reload that. The saver doesn't save the graph structure, so we need to build one with identical node names and reload it here.
	# Right now the graph can be loaded with tf.import_graph_def OR the variables can be populated with Restore, but not both (yet).
	# The graph.pbtxt holds graph structure (in model folder).  model-checkpoint has values/weights.
	# TODO: Review when bug is fixed. (2015/11/29)
	if os.path.isfile("./model/checkpoint.model"):
		print("Restored model state.")
		saver.restore(sess, "./model/checkpoint.model")

	# Begin training
	for iteration in range(TRAINING_ITERATIONS):
		x_batch = generator.next()
		sess.run(optimizer, feed_dict={input_batch:x_batch})
		if iteration % TRAINING_REPORT_INTERVAL == 0:
			# Checkpoint progress
			print("Finished batch {}".format(iteration))
			saver.save(sess, "./model/checkpoint.model", global_step=iteration)

			# Render output sample
			#encoded, decoded = sess.run([encoder, decoder], feed_dict={input_batch:x_batch, encoded_batch:np.random.uniform(size=(BATCH_SIZE, REPRESENTATION_SIZE))})
			encoded = sess.run(encoder, feed_dict={input_batch:x_batch})

			# Randomly generated sample
			#decoded = sess.run(decoder, feed_dict={encoded_batch:np.random.normal(loc=encoded.mean(), scale=encoded.std(), size=[BATCH_SIZE, REPRESENTATION_SIZE])})
			decoded = sess.run(decoder, feed_dict={encoded_batch:np.random.uniform(low=encoded.min(), high=encoded.max(), size=[BATCH_SIZE, REPRESENTATION_SIZE])})
			#img_tensor = tf.image.encode_jpeg(decoded[0])
			decoded_norm = (decoded[0]-decoded.min())/(decoded.max()-decoded.min())
			img_arr = np.asarray(decoded_norm*255, dtype=np.uint8)
			if IMAGE_DEPTH == 3:
				img = Image.fromarray(img_arr)
			else:
				img = Image.fromarray(img_arr[:,:,0])
			img.save("test_{}.jpg".format(iteration))

			# Reconstructed sample ends up looking just like the random sample, so don't waste time making it.