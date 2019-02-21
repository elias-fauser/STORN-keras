import keras.backend as K


def sample_gauss(mu, sig, batch_size, dim_size):
    epsilon = K.random_normal(shape=(batch_size,
                                     dim_size),
                              mean=0., stddev=1.,
                              dtype="float32")
    sample = mu + sig * epsilon
    return sample

def sample_bernoulli(p, batch_size, dim_size):
    uni = K.random_uniform((batch_size, dim_size))
    return K.less(uni, p)