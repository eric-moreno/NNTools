from __future__ import print_function
import mxnet as mx
import logging
import os
import time

def _get_lr_scheduler(args, kv):
    if 'lr_factor' not in args or args.lr_factor >= 1:
        return (args.lr, None)
    epoch_size = args.num_examples // args.batch_size
    if 'dist' in args.kv_store:
        epoch_size //= kv.num_workers
    begin_epoch = args.load_epoch if args.load_epoch else 0
    step_epochs = [int(l) for l in args.lr_step_epochs.split(',')]
    lr = args.lr
    for s in step_epochs:
        if begin_epoch >= s:
            lr *= args.lr_factor
    if lr != args.lr:
        logging.info('Adjust learning rate to %e for epoch %d' %(lr, begin_epoch))

    steps = [epoch_size * (x-begin_epoch) for x in step_epochs if x-begin_epoch > 0]
    return (lr, mx.lr_scheduler.MultiFactorScheduler(step=steps, factor=args.lr_factor))

def _load_model(args, rank=0):
    if 'load_epoch' not in args or args.load_epoch is None:
        return (None, None, None)
    assert args.model_prefix is not None
    model_prefix = args.model_prefix
    if rank > 0 and os.path.exists("%s-%d-symbol.json" % (model_prefix, rank)):
        model_prefix += "-%d" % (rank)
    sym, arg_params, aux_params = mx.model.load_checkpoint(
        model_prefix, args.load_epoch)
    logging.info('Loaded model %s_%04d.params', model_prefix, args.load_epoch)
    return (sym, arg_params, aux_params)

def _save_model(args, rank=0):
    if args.model_prefix is None:
        return None
    dst_dir = os.path.dirname(args.model_prefix)
    if not os.path.exists(dst_dir):
        os.makedirs(dst_dir)
    return mx.callback.do_checkpoint(args.model_prefix if rank == 0 else "%s-%d" % (
        args.model_prefix, rank))

def add_fit_args(parser):
    """
    parser : argparse.ArgumentParser
    return a parser added with args required by fit
    """
    train = parser.add_argument_group('Training', 'model training')
    train.add_argument('--network', type=str,
                       help='the neural network to use')
    train.add_argument('--num-layers', type=int,
                       help='number of layers in the neural network, required by some networks such as resnet')
    train.add_argument('--gpus', type=str, default='0',
                       help='list of gpus to run, e.g. 0 or 0,2,5. empty means using cpu')
    train.add_argument('--kv-store', type=str, default='device',
                       help='key-value store type')
    train.add_argument('--num-epochs', type=int, default=500,
                       help='max num of epochs')
    train.add_argument('--lr', type=float, default=0.1,
                       help='initial learning rate')
    train.add_argument('--lr-factor', type=float, default=0.1,
                       help='the ratio to reduce lr on each step')
    train.add_argument('--lr-step-epochs', type=str,
                       help='the epochs to reduce the lr, e.g. 30,60')
    train.add_argument('--optimizer', type=str, default='sgd',
                       help='the optimizer type')
    train.add_argument('--mom', type=float, default=0.9,
                       help='momentum for sgd')
    train.add_argument('--wd', type=float, default=0.0001,
                       help='weight decay for sgd')
    train.add_argument('--batch-size', type=int, default=128,
                       help='the batch size')
    train.add_argument('--disp-batches', type=int, default=20,
                       help='show progress for every n batches')
    train.add_argument('--model-prefix', type=str,
                       help='model prefix')
    parser.add_argument('--monitor', dest='monitor', type=int, default=0,
                        help='log network parameters every N iters if larger than 0')
    train.add_argument('--load-epoch', type=int,
                       help='load the model on an epoch using the model-load-prefix')
    train.add_argument('--top-k', type=int, default=0,
                       help='report the top-k accuracy. 0 means no report.')
    train.add_argument('--test-io', action='store_true', default=False,
                       help='test reading speed without training')
    train.add_argument('--predict', action='store_true', default=False,
                       help='run prediction instead of training')
    train.add_argument('--predict-output', type=str,
                       help='predict output')
    return train

class dummyKV:
    def __init__(self):
        self.rank = 0

def fit(args, network, data_loader, **kwargs):
    """
    train a model
    args : argparse returns
    network : the symbol definition of the nerual network
    data_loader : function that returns the train and val data iterators
    """
    # kvstore
    if args.gpus is None or len(args.gpus.split(',')) <= 1:
        use_kv = False
        print('Single device. Do not use KVStore.')
        kv = dummyKV()
    else:
        use_kv = True
        kv = mx.kvstore.create(args.kv_store)

    # logging
    head = '%(asctime)-15s Node[' + str(kv.rank) + '] %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=head)
    logging.info('start with arguments %s', args)

    # data iterators
    (train, val) = data_loader(args)
    if args.test_io:
        for i_epoch in range(args.num_epochs):
            train.reset()
            tic = time.time()
            for i, batch in enumerate(train):
                for j in batch.data:
                    j.wait_to_read()
                if (i + 1) % args.disp_batches == 0:
                    logging.info('Epoch [%d]/Batch [%d]\tSpeed: %.2f samples/sec' % (
                        i_epoch, i, args.disp_batches * args.batch_size / (time.time() - tic)))
                    tic = time.time()

        return

    logging.info('Data shape:\n' + str(train.provide_data))
    logging.info('Label shape:\n' + str(train.provide_label))

    # load model
    if 'arg_params' in kwargs and 'aux_params' in kwargs:
        arg_params = kwargs['arg_params']
        aux_params = kwargs['aux_params']
    else:
        sym, arg_params, aux_params = _load_model(args, kv.rank)
        if sym is not None:
            assert sym.tojson() == network.tojson()

    # save model
    checkpoint = _save_model(args, kv.rank)
    if args.dryrun:
        checkpoint = None

    # devices for training
    devs = mx.cpu() if args.gpus is None or args.gpus is '' else [
        mx.gpu(int(i)) for i in args.gpus.split(',')]

    # learning rate
    lr, lr_scheduler = _get_lr_scheduler(args, kv)

    # create model
    model = mx.mod.Module(
        context       = devs,
        symbol        = network,
        data_names    = args.data_names.split(','),
        label_names   = args.label_names.split(','),
    )

    optimizer_params = {
            'learning_rate': lr,
            'lr_scheduler': lr_scheduler}
    if args.optimizer == 'sgd':
        optimizer_params['momentum'] = args.mom
        optimizer_params['wd'] = args.wd

    monitor = mx.mon.Monitor(args.monitor, pattern=".*") if args.monitor > 0 else None

    if args.network == 'alexnet':
        # AlexNet will not converge using Xavier
        initializer = mx.init.Normal()
    else:
        initializer = mx.init.Xavier(
            rnd_type='gaussian', factor_type="in", magnitude=2)
    # initializer   = mx.init.Xavier(factor_type="in", magnitude=2.34),

    # evaluation metrices
    eval_metrics = ['accuracy', 'ce']
    if args.top_k > 0:
        eval_metrics.append(mx.metric.create('top_k_accuracy', top_k=args.top_k))

    # callbacks that run after each batch
    batch_end_callbacks = [mx.callback.Speedometer(args.batch_size, args.disp_batches, auto_reset=True)]
    if 'batch_end_callback' in kwargs:
        cbs = kwargs['batch_end_callback']
        batch_end_callbacks += cbs if isinstance(cbs, list) else [cbs]

    eval_batch_end_callback = [mx.callback.Speedometer(args.batch_size, args.disp_batches * 10, False)]

    # run
    logging.info('Start training...')
    model.fit(train,
        begin_epoch        = args.load_epoch if args.load_epoch else 0,
        num_epoch          = args.num_epochs,
        eval_data          = val,
        eval_metric        = eval_metrics,
        kvstore            = kv if use_kv else None,
        optimizer          = args.optimizer,
        optimizer_params   = optimizer_params,
        initializer        = initializer,
        arg_params         = arg_params,
        aux_params         = aux_params,
        batch_end_callback = batch_end_callbacks,
        epoch_end_callback = checkpoint,
        eval_batch_end_callback = eval_batch_end_callback,
        allow_missing      = True,
        monitor            = monitor)


def predict(args, data_loader, **kwargs):
    """
    predict with a trained a model
    args : argparse returns
    data_loader : function that returns the train and val data iterators
    """
    # kvstore
    kv = mx.kvstore.create(args.kv_store)

    # logging
    head = '%(asctime)-15s Node[' + str(kv.rank) + '] %(message)s'
    logging.basicConfig(level=logging.DEBUG, format=head)
    logging.info('start with arguments %s', args)

    # data iterators
    data_iter = data_loader(args)

    # load model
    sym, arg_params, aux_params = _load_model(args, kv.rank)

    # devices for training
    devs = mx.cpu() if args.gpus is None or args.gpus is '' else [
        mx.gpu(int(i)) for i in args.gpus.split(',')]

    # create model
    model = mx.mod.Module(
        context=devs,
        symbol=sym,
        data_names=args.data_names.split(','),
        label_names=args.label_names.split(','),
    )
    model.bind(for_training=False, data_shapes=data_iter.provide_data, label_shapes=data_iter.provide_label)
    model.set_params(arg_params, aux_params)
    preds = model.predict(data_iter).asnumpy()
    truths = data_iter.get_truths()
    observers = data_iter.get_observers()

    print(preds.shape, truths.shape, observers.shape)

    pred_output = {}
    for i in range(args.num_classes):
        pred_output['class_%d' % i] = truths[:, i]
        pred_output['score_%d' % i] = preds[:, i]
    for i, obs in enumerate(data_iter.d.obs_vars):
        pred_output[obs] = observers[:, i]

    import pandas as pd
    df = pd.DataFrame(pred_output)
    if args.predict_output:
        logging.info('Write prediction file to %s' % args.predict_output)
        outdir = os.path.dirname(args.predict_output)
        if not os.path.exists(outdir):
            os.makedirs(outdir)
        df.to_hdf(args.predict_output, 'Events', format='table')

        from common.util import plotROC
        plotROC(preds, truths, output=os.path.join(outdir, 'roc.pdf'))

        from root_numpy import array2root
        array2root(df.to_records(index=False), filename=args.predict_output.rsplit('.', 1)[0] + '.root', treename='Events', mode='RECREATE')