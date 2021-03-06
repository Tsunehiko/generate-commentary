import argparse
import time
import os
import copy
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from tqdm import tqdm
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import numpy as np
import torchvision
from torchvision import datasets, models, transforms
from torch.utils.tensorboard import SummaryWriter

from dataset import FrameDataset

from logging import getLogger, StreamHandler, Formatter, FileHandler, DEBUG, INFO, WARNING, ERROR, CRITICAL


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch Implementation of video-clustering')

    parser.add_argument('--video-path', type=str, help='path to dataset(videos)')
    parser.add_argument('--frame-path', type=str, help='path to dataset(frames)')
    parser.add_argument('--label-path', type=str, help='path to label')
    parser.add_argument('--log', type=str, help='path to log')
    parser.add_argument('--workers', default=4, type=int,
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--batch', default=256, type=int,
                        help='mini-batch size (default: 256)')
    parser.add_argument('--seed', type=int, default=31, help='random seed (default: 31)')
    parser.add_argument('--epochs', default=200, type=int, help='epoch size(default: 200)')
    parser.add_argument('--model-name', type=str)
    parser.add_argument('--feature-extract', help='Flag for feature extracting. When False, we finetune the whole model, when True we only update the reshaped layer params')
    parser.add_argument('--interval', type=int, help='interval of saving model')
    parser.add_argument('--threshold', type=float)
    parser.add_argument('--name', type=str, help='experiment name')
    parser.add_argument('--lr', type=float, help='optimizer learning rate')
    return parser.parse_args()


def main(args, logger):

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    np.random.seed(args.seed)

    feature_extract = args.feature_extract

    writer_dir = os.path.join(args.log, "tensorboard")
    # writer_dir = os.path.join('./2D/results/tensorboard/', args.name)
    if not os.path.exists(writer_dir):
        os.makedirs(writer_dir)
    writers = {x : SummaryWriter(log_dir=os.path.join(writer_dir, x)) for x in ['train', 'valid']}

    model, input_size = initialize_model(args.model_name, 1, feature_extract)
    logger.info(f"{torch.cuda.device_count()} GPUs are being used.")
    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)
    model = model.to(device)
    cudnn.benchmark = True

    transform = transforms.Compose([transforms.RandomResizedCrop(input_size),
                                    transforms.RandomHorizontalFlip(),
                                    transforms.ToTensor(),
                                    ])

    video_paths = {x: os.path.join(args.video_path, x) for x in ['train', 'valid']}
    frame_paths = {x: os.path.join(args.frame_path, x) for x in ['train', 'valid']}
    label_paths = {x: os.path.join(args.label_path, f"{x}.csv") for x in ['train', 'valid']}
    calc_paths = {x: os.path.join(args.log, f"calc/{x}") for x in ['train', 'valid']}

    datasets = {x: FrameDataset(video_paths[x], frame_paths[x], label_paths[x], calc_paths[x], input_size, transform=transform) for x in ['train', 'valid']}

    dataloaders = {x: torch.utils.data.DataLoader(datasets[x], batch_size=args.batch, num_workers=args.workers, pin_memory=True, shuffle=True) for x in ['train', 'valid']}

    params_to_update = model.parameters()
    logger.info("Params to learn:")
    if feature_extract:
        params_to_update = []
        for name,param in model.named_parameters():
            if param.requires_grad == True:
                params_to_update.append(param)
                logger.info(name)
    else:
        for name,param in model.named_parameters():
            if param.requires_grad == True:
                logger.info(name)
    optimizer = optim.SGD(params_to_update, lr=args.lr, momentum=0.7)
    # optimizer = optim.Adam(params_to_update, lr=0.001, weight_decay=0)

    start_epoch = 0
    model_save_dir = os.path.join(args.log, 'models')
    if os.path.exists(model_save_dir) and len(os.listdir(model_save_dir)) > 0:
        logger.debug("Load pretrained model...")
        latest_path = os.path.join(model_save_dir, sorted(os.listdir(model_save_dir))[-1])
        checkpoint = torch.load(latest_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']

    criterion = nn.BCEWithLogitsLoss()
    
    train(model, dataloaders, criterion, optimizer, args.epochs, model_save_dir, args.interval, writers, start_epoch=start_epoch, is_inception=(args.model_name=="inception"), threshold=args.threshold)

    for x in ['train', 'valid']:
        writers[x].close()



def train(model, dataloaders, criterion, optimizer, num_epochs,  model_save_dir, interval, writers, start_epoch=0, is_inception=False, threshold=0.5):
    since = time.time()

    best_pre_acc = 0.0

    for epoch in range(start_epoch, num_epochs):
        logger.info("-" * 20)
        logger.info("Epoch {} / {}".format(epoch, args.epochs - 1))

        for phase in ['train', 'valid']:
            if phase == 'train':
                model.train()
            else:
                model.eval()

            running_loss = 0.0
            running_corrects = 0
            tp = 0
            tn = 0
            fp = 0
            fn = 0

            for inputs, labels, vnames in dataloaders[phase]:
                inputs = inputs.to(device)
                labels = labels.to(device)

                optimizer.zero_grad()

                with torch.set_grad_enabled(phase == 'train'):
                    
                    if is_inception and phase == 'train':
                        outputs, aux_outputs = model(inputs)
                        loss1 = criterion(outputs, labels)
                        loss2 = criterion(aux_outputs, labels)
                        loss = loss1 + 0.4*loss2
                    else:
                        outputs = model(inputs)
                        labels = labels.type_as(outputs)
                        loss = criterion(outputs.view(-1), labels)
                    
                    preds = torch.sigmoid(outputs.view(-1)) > threshold

                    if phase == 'train':
                        loss.backward()
                        optimizer.step()

                running_loss += loss.item() * inputs.size(0)
                running_corrects += torch.sum(preds.data == labels.data)
                tp += torch.sum((preds.data == 1.0) & (labels.data == 1.0))
                tn += torch.sum((preds.data == 0.0) & (labels.data == 1.0))
                fp += torch.sum((preds.data == 1.0) & (labels.data == 0.0))
                fn += torch.sum((preds.data == 0.0) & (labels.data == 0.0))

            epoch_loss = running_loss / len(dataloaders[phase].dataset)
            epoch_acc = running_corrects.double() / len(dataloaders[phase].dataset)
            precision = tp.double() / (tp.double() + fp.double())
            recall = tp.double() / (tp.double() + fn.double())
            f1 = 2 * tp.double() / (2 * tp.double() + fp + fn)


            writers[phase].add_scalar("loss", epoch_loss, epoch)
            writers[phase].add_scalar("accuracy", epoch_acc, epoch)
            writers[phase].add_scalar("precision", precision, epoch)
            writers[phase].add_scalar("recall", recall, epoch)
            writers[phase].add_scalar("f1_score", f1, epoch)

            logger.info("{} Loss: {:.7f} Acc:{:.7f} Precision:{:.7f} Recall:{:.7f} F1:{:.7f}"
                    .format(phase, epoch_loss, epoch_acc, precision, recall, f1))

        if phase == 'valid' and best_pre_acc < precision:
            best_pre_acc = precision
            logger.info("Saving best model...")
            if not os.path.exists(model_save_dir):
                os.makedirs(model_save_dir)
            torch.save({
                        'epoch': epoch,
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                        'loss': loss
                        },os.path.join(model_save_dir, "best.pkl"))

        if epoch % interval == 0:
            logger.info("Saving model...")
            if not os.path.exists(model_save_dir):
                os.makedirs(model_save_dir)
            torch.save({
                        'epoch': epoch,
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                        'loss': loss
                        },os.path.join(model_save_dir, f"Epoch-{epoch}.pkl"))

    time_elapsed = time.time() - since
    logger.info('Training complete in {:.0f}m {:.0f}s'.format(time_elapsed // 60, time_elapsed % 60))


def set_parameter_requires_grad(model, feature_extract):
    if feature_extract:
        for param in model.parameters():
            param.requires_grad = False


def initialize_model(model_name, num_classes, feature_extract=False, use_pretrained=True):
    model_ft = None
    input_size = 0

    if model_name == 'resnet18':
        """ Resnet18
        """
        model_ft = models.resnet18(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.fc.in_features
        model_ft.fc = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == 'resnet101':
        """ Resnet101
        """
        model_ft = models.resnet101(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.fc.in_features
        model_ft.fc = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == 'resnext':
        """ ResNext50_32x4d
        """
        model_ft = models.resnext50_32x4d(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.fc.in_features
        model_ft.fc = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == 'alexnet':
        """ Alexnet
        """
        model_ft = models.alexnet(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.classifier[6].in_features
        model_ft.classifier[6] = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == 'vgg':
        """ VGG11_bn
        """
        model_ft = models.vgg11_bn(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.classifier[6].in_features
        model_ft.classifier[6] = nn.Linear(num_ftrs, num_classes)
        input_size = 224

    elif model_name == 'squeezenet':
        """ Squeezenet
        """
        model_ft = models.squeezenet1_0(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        model_ft.classifier[1] = nn.Conv2d(512, num_classes, kernel_size=(1,1), stride=(1,1))
        model_ft.num_classes = num_classes
        input_size = 224

    elif model_name == 'inception':
        """ Inception v3
        Be careful, expects (299,299) sized images and has auxiliary output
        """
        model_ft = models.inception_v3(pretrained=use_pretrained)
        set_parameter_requires_grad(model_ft, feature_extract)
        num_ftrs = model_ft.AuxLogits.fc.in_features
        model_ft.AuxLogits.fc = nn.Linear(num_ftrs, num_classes)
        num_ftrs = model_ft.fc.in_features
        model_ft.fc = nn.Linear(num_ftrs, num_classes)
        input_size = 299

    else:
        logger.warning("Invalid model name, exiting...")
        exit()
    
    return model_ft, input_size


def init_logger(log_path, modname=__name__):
    logger = getLogger('log')
    logger.setLevel(DEBUG)

    sh = StreamHandler()
    sh_formatter = Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    sh.setFormatter(sh_formatter)
    logger.addHandler(sh)

    fh = FileHandler(log_path)
    fh.setLevel(INFO)
    fh_formatter = Formatter('%(asctime)s - %(filename)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s')
    fh.setFormatter(fh_formatter)
    logger.addHandler(fh)
    
    return logger

if __name__ == '__main__':
    args = parse_args()
    if not os.path.exists(args.log):
        os.makedirs(args.log)
    logger = init_logger(f"{args.log}/result.log")
    main(args, logger)